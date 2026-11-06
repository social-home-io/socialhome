"""Who may put a blob into — or take one out of — the space media cache.

Identity-free by design: nothing here looks up which household is asking.
An upload is authorized by exactly what this server already verifies for the
scope's other traffic (:mod:`socialhome.domain.gfs_space_media` has the wire
shape):

**Scope ``space``** (a listed space):

* **space authority** — the signature ``/gfs/publish`` relays are authorized
  by, verified against the space's TOFU-pinned key
  (:meth:`.federation.GfsFederationService.verify_space_authority`), under
  its own event type so a relay payload can never authorize an upload;
* **trusted-mode writer cert** (v_49) — the cert ``/gfs/member-publish``
  checks (:meth:`.member_publish.GfsMemberPublishService.verify_writer_cert`:
  pinned authority key, epoch open, space not strict), held by whoever
  signs the claim with the household key the cert names;
* **strict-mode writer key** (v_50) — the writer group key
  ``/gfs/member-publish-anon`` checks
  (:meth:`~.member_publish.GfsMemberPublishService.verify_writer_key_sig`).

The space must be listed here, not moderator-banned, with a pinned key.

**Scope ``channel``** (a private space's opaque v_51 channel — this server
knows only the random channel id and the keys pinned for it):

* **channel key** — the key registration, epoch notices and unregister are
  signed with (:meth:`.channels.GfsChannelService.verify_channel_key`);
* **channel cert** — the cert a trusted channel publish carries
  (:meth:`~.channels.GfsChannelService.verify_cert_holder`), held by whoever
  signs the claim with the household key the cert names;
* **channel writer key** — the strict channel's per-epoch writer key
  (:meth:`~.channels.GfsChannelService.verify_writer_key`).

The claim's ``ts`` must be within :data:`SPACE_MEDIA_TS_SKEW_S` of this
server's clock. A captured upload replayed inside that window is harmless:
the blob is content-addressed, so it is the same bytes again (a no-op), or
lands on another GFS serving the same scope, inside that scope's caps there.

A delete is the scope's seed holder's alone: the space authority
(``space_media_delete``) or the channel key. Every refusal is a
:class:`PermissionError`; the route answers all of them with one uniform 403.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..authority_sig import (
    AUTHORITY_EVENT_SPACE_MEDIA_DELETE,
    AUTHORITY_EVENT_SPACE_MEDIA_PUT,
)
from ..crypto import b64url_decode, verify_ed25519
from ..domain.gfs_space_media import (
    AUTH_AUTHORITY,
    AUTH_CHANNEL,
    AUTH_CHANNEL_CERT,
    AUTH_KINDS_BY_SCOPE,
    AUTH_WRITER_CERT,
    AUTH_WRITER_KEY,
    CHANNEL_MEDIA_DELETE_PREFIX,
    CHANNEL_MEDIA_PUT_PREFIX,
    SCOPE_CHANNEL,
    SCOPE_SPACE,
    SpaceMediaUploadAuth,
    UnsupportedSpaceMediaSigSuite,
    media_delete_payload,
    require_space_media_sig_suite,
)
from .federation import TIMING_UNIFORM_DUMMY_KEY_HEX, TIMING_UNIFORM_DUMMY_SIG
from .public import ClientIpResolver, build_window_limiter

if TYPE_CHECKING:
    from .channels import GfsChannelService
    from .domain import GlobalSpace
    from .federation import GfsFederationService
    from .member_publish import GfsMemberPublishService

#: Uploads + deletes per client address per minute, shed BEFORE any
#: signature work (the per-scope caps need an authorized request first).
#: A busy household uploads a handful of pictures per post.
SPACE_MEDIA_MAX_PER_MINUTE_PER_IP: int = 120

#: Ed25519 verifications each lookup-gated kind makes once its scope is
#: found — what an early refusal burns instead.
_VERIFIES_BY_KIND: dict[str, int] = {
    AUTH_CHANNEL: 1,
    AUTH_CHANNEL_CERT: 2,
    AUTH_WRITER_CERT: 2,
    AUTH_WRITER_KEY: 1,
}

_BURN_MESSAGE: bytes = b"space-media-timing-burn"

#: How far an upload's or delete's ``ts`` may be from this server's clock —
#: the window of every signed household request.
SPACE_MEDIA_TS_SKEW_S: int = 300


class GfsSpaceMediaAuth:
    """Authorize uploads to and deletes from the space media cache."""

    __slots__ = ("_channels", "_clock", "_federation", "_member_publish")

    def __init__(
        self,
        *,
        federation: GfsFederationService,
        member_publish: GfsMemberPublishService,
        channels: GfsChannelService,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._federation = federation
        self._member_publish = member_publish
        self._channels = channels
        self._clock = clock

    async def authorize_upload(
        self,
        auth: SpaceMediaUploadAuth,
        *,
        scope_kind: str,
        scope_id: str,
        blob_id: str,
    ) -> None:
        """Raise :class:`PermissionError` unless *auth* lets its holder put
        *blob_id* into the scope the path names (the signed claim must name
        the same)."""
        claim = auth.claim
        if (claim.scope, claim.scope_id, claim.blob_id) != (
            scope_kind,
            scope_id,
            blob_id,
        ):
            raise PermissionError("claim names another scope or blob")
        if auth.kind not in AUTH_KINDS_BY_SCOPE.get(scope_kind, frozenset()):
            raise PermissionError("authorization kind not taken for this scope")
        self._require_fresh(claim.ts)
        space: GlobalSpace | None = None
        if auth.kind != AUTH_AUTHORITY:
            # The scope lookup first, and a refusal costs what the skipped
            # verifications would have, so timing doesn't tell which spaces
            # are listed or banned, or which channels exist (the authority
            # path burns its own in ``verify_space_authority``).
            try:
                if scope_kind == SCOPE_SPACE:
                    space = await self._federation.listed_space(scope_id)
                else:
                    await self._channels.require_channel(scope_id)
            except PermissionError:
                _burn_verifies(_VERIFIES_BY_KIND[auth.kind])
                raise
        if auth.kind == AUTH_AUTHORITY:
            await self._federation.verify_space_authority(
                scope_id,
                event_type=AUTHORITY_EVENT_SPACE_MEDIA_PUT,
                payload=claim.signing_body(),
                authority_sig=auth.sig,
                authority_sig_suite=auth.sig_suite,
            )
            return
        if auth.kind == AUTH_CHANNEL:
            await self._channels.verify_channel_key(
                scope_id,
                prefix=CHANNEL_MEDIA_PUT_PREFIX,
                body=claim.signing_body(),
                signature=auth.sig,
                suite=auth.sig_suite,
            )
            return
        if auth.epoch is None:  # pragma: no cover - the codec requires one
            raise PermissionError("writer upload without an epoch")
        message = claim.writer_signing_bytes(kind=auth.kind, epoch=auth.epoch)
        if auth.kind == AUTH_WRITER_CERT and auth.writer_cert is not None:
            if space is None:  # pragma: no cover - looked up above
                raise PermissionError("space not looked up")
            author_pk = self._cert_holder(auth, auth.writer_cert.instance_pk, message)
            await self._member_publish.verify_writer_cert(
                space, auth.writer_cert, epoch=auth.epoch, author_pk=author_pk
            )
            return
        if auth.kind == AUTH_CHANNEL_CERT and auth.channel_cert is not None:
            author_pk = self._cert_holder(auth, auth.channel_cert.instance_pk, message)
            await self._channels.verify_cert_holder(
                scope_id, auth.channel_cert, epoch=auth.epoch, author_pk=author_pk
            )
            return
        if auth.kind == AUTH_WRITER_KEY and scope_kind == SCOPE_SPACE:
            await self._member_publish.verify_writer_key_sig(
                scope_id,
                epoch=auth.epoch,
                message=message,
                writer_sig=auth.sig,
                suite=auth.sig_suite,
            )
            await self._member_publish.require_writer_key_epoch(scope_id, auth.epoch)
            return
        if auth.kind == AUTH_WRITER_KEY and scope_kind == SCOPE_CHANNEL:
            await self._channels.verify_writer_key(
                scope_id,
                epoch=auth.epoch,
                message=message,
                writer_sig=auth.sig,
                suite=auth.sig_suite,
            )
            return
        raise PermissionError("unknown upload authorization")  # pragma: no cover

    async def authorize_delete(
        self,
        scope_kind: str,
        scope_id: str,
        blob_id: str,
        *,
        ts: object,
        sig: object,
        sig_suite: object,
    ) -> None:
        """Raise :class:`PermissionError` unless the scope's seed holder (the
        space authority, or the channel key) signed this delete of *blob_id*
        within the skew window."""
        if not isinstance(ts, int) or isinstance(ts, bool):
            raise PermissionError("bad ts")
        self._require_fresh(ts)
        if not isinstance(sig, str) or not isinstance(sig_suite, str):
            raise PermissionError("bad signature")
        payload = media_delete_payload(scope_kind, scope_id, blob_id, ts)
        if scope_kind == SCOPE_SPACE:
            await self._federation.verify_space_authority(
                scope_id,
                event_type=AUTHORITY_EVENT_SPACE_MEDIA_DELETE,
                payload=payload,
                authority_sig=sig,
                authority_sig_suite=sig_suite,
            )
        elif scope_kind == SCOPE_CHANNEL:
            await self._channels.verify_channel_key(
                scope_id,
                prefix=CHANNEL_MEDIA_DELETE_PREFIX,
                body=payload,
                signature=sig,
                suite=sig_suite,
            )
        else:
            raise PermissionError("unknown scope")

    @staticmethod
    def _cert_holder(
        auth: SpaceMediaUploadAuth, instance_pk: str, message: bytes
    ) -> bytes:
        """The household key a cert names, once ``auth.sig`` proves the
        uploader holds it. :class:`PermissionError` otherwise."""
        try:
            require_space_media_sig_suite(auth.sig_suite)
            author_pk = b64url_decode(instance_pk)
            ok = verify_ed25519(author_pk, message, b64url_decode(auth.sig))
        except (UnsupportedSpaceMediaSigSuite, ValueError, TypeError) as exc:
            raise PermissionError(f"cert holder signature refused: {exc}") from exc
        if not ok:
            raise PermissionError("cert holder signature does not verify")
        return author_pk

    def _require_fresh(self, ts: int) -> None:
        if abs(ts - self._clock()) > SPACE_MEDIA_TS_SKEW_S:
            raise PermissionError("stale or future timestamp")


def _burn_verifies(n: int) -> None:
    """Make *n* throw-away Ed25519 verifications against the fixed dummy key
    the other early-refusal paths use; the result is discarded."""
    key = bytes.fromhex(TIMING_UNIFORM_DUMMY_KEY_HEX)
    for _ in range(n):
        verify_ed25519(key, _BURN_MESSAGE, TIMING_UNIFORM_DUMMY_SIG)


def is_space_media_write_path(path: str) -> bool:
    """``/gfs/{spaces|channels}/{id}/media/{blob}`` and its ``/delete`` — not
    the public fetch ``/gfs/media/{blob}`` (a CDN serves most of those)."""
    parts = path.split("/")
    return (
        len(parts) in (6, 7)
        and parts[1] == "gfs"
        and parts[2] in ("spaces", "channels")
        and parts[4] == "media"
        and (len(parts) == 6 or parts[6] == "delete")
    )


def build_space_media_rate_limit(resolver: ClientIpResolver):
    """Per-IP limiter for the space media upload and delete routes."""
    return build_window_limiter(
        resolver, SPACE_MEDIA_MAX_PER_MINUTE_PER_IP, is_space_media_write_path
    )
