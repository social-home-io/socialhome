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
the blob's address binds its bytes and scope, so it is the same blob again (a no-op), or
lands on another GFS serving the same scope, inside that scope's caps there.

A delete is the scope's seed holder's alone: the space authority
(``space_media_delete``) or the channel key. A delete is taken ONCE: an
exact replay inside the ±300 s window is refused (a seen-digest cache, per
node; a replay to another node can only delete that scope's own blob,
which its seed holder deleted already — the household does not upload a
deleted item's file again).
Every refusal is a :class:`PermissionError`; the route answers all of them
with one uniform 403.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from aiohttp import web

from ..authority_sig import (
    AUTHORITY_EVENT_SPACE_MEDIA_DELETE,
    AUTHORITY_EVENT_SPACE_MEDIA_PUT,
)
from ..crypto import b64url_decode, counting_ed25519_verifies, verify_ed25519
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
from .federation import (
    TIMING_UNIFORM_DUMMY_KEY_HEX,
    TIMING_UNIFORM_DUMMY_SIG,
    SeenPayloadCache,
)
from .public import ClientIpResolver, SlidingWindowCounter, node_share

if TYPE_CHECKING:
    from .channels import GfsChannelService
    from .domain import GlobalSpace
    from .federation import GfsFederationService
    from .member_publish import GfsMemberPublishService

#: Uploads + deletes per client address per minute, shed BEFORE any
#: signature work (the per-scope caps need an authorized request first).
#: A busy household uploads a handful of pictures per post.
SPACE_MEDIA_MAX_PER_MINUTE_PER_IP: int = 120

#: Ed25519 verifications a successful upload of each kind makes. Every
#: refusal of that kind is topped up to the same count (dummy verifications
#: against a fixed key), so a refusal's timing tells nothing about which
#: spaces are listed or banned, which channels exist, which writer keys are
#: pinned or which mode a space is in. A delete makes one.
_VERIFIES_BY_KIND: dict[str, int] = {
    AUTH_AUTHORITY: 1,
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

    __slots__ = (
        "_channels",
        "_clock",
        "_deletes_seen",
        "_federation",
        "_member_publish",
    )

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
        #: Deletes already taken (their signed bytes' digest), for the
        #: skew window on both sides.
        self._deletes_seen = SeenPayloadCache(ttl_s=float(2 * SPACE_MEDIA_TS_SKEW_S))

    async def authorize_upload(
        self,
        auth: SpaceMediaUploadAuth,
        *,
        scope_kind: str,
        scope_id: str,
        blob_id: str,
    ) -> str:
        """Raise :class:`PermissionError` unless *auth* lets its holder put
        *blob_id* into the scope the claim names. Returns the space's owning
        household — the cache's fairness key — or ``""`` for a channel (its
        blobs count against their uploader's address bucket, which the route
        knows). Every refusal
        costs the verifications a success of that kind makes."""
        with counting_ed25519_verifies() as done:
            try:
                return await self._authorize_upload(
                    auth, scope_kind=scope_kind, scope_id=scope_id, blob_id=blob_id
                )
            except PermissionError:
                _burn_verifies(_VERIFIES_BY_KIND.get(auth.kind, 1) - done[0])
                raise

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
        within the skew window. Every refusal costs one verification, like
        a success."""
        with counting_ed25519_verifies() as done:
            try:
                await self._authorize_delete(
                    scope_kind, scope_id, blob_id, ts=ts, sig=sig, sig_suite=sig_suite
                )
            except PermissionError:
                _burn_verifies(1 - done[0])
                raise

    async def _authorize_upload(
        self,
        auth: SpaceMediaUploadAuth,
        *,
        scope_kind: str,
        scope_id: str,
        blob_id: str,
    ) -> str:
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
        household = ""
        if scope_kind == SCOPE_CHANNEL:
            # The scope first (its refusal is topped up by the caller): a
            # channel must be IN USE — a member household holds a seat —
            # for every kind, the channel key's included (registration is
            # anonymous).
            await self._channels.require_channel_in_use(scope_id)
        elif auth.kind != AUTH_AUTHORITY:
            space = _active(await self._federation.listed_space(scope_id))
            household = space.owning_instance
        if auth.kind == AUTH_AUTHORITY:
            space = await self._federation.verify_space_authority(
                scope_id,
                event_type=AUTHORITY_EVENT_SPACE_MEDIA_PUT,
                payload=claim.signing_body(),
                authority_sig=auth.sig,
                authority_sig_suite=auth.sig_suite,
            )
            return _active(space).owning_instance
        if auth.kind == AUTH_CHANNEL:
            await self._channels.verify_channel_key(
                scope_id,
                prefix=CHANNEL_MEDIA_PUT_PREFIX,
                body=claim.signing_body(),
                signature=auth.sig,
                suite=auth.sig_suite,
            )
            return household
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
            return household
        if auth.kind == AUTH_CHANNEL_CERT and auth.channel_cert is not None:
            author_pk = self._cert_holder(auth, auth.channel_cert.instance_pk, message)
            await self._channels.verify_cert_holder(
                scope_id, auth.channel_cert, epoch=auth.epoch, author_pk=author_pk
            )
            return household
        if auth.kind == AUTH_WRITER_KEY and scope_kind == SCOPE_SPACE:
            await self._member_publish.verify_writer_key_sig(
                scope_id,
                epoch=auth.epoch,
                message=message,
                writer_sig=auth.sig,
                suite=auth.sig_suite,
            )
            await self._member_publish.require_writer_key_epoch(scope_id, auth.epoch)
            return household
        if auth.kind == AUTH_WRITER_KEY and scope_kind == SCOPE_CHANNEL:
            await self._channels.verify_writer_key(
                scope_id,
                epoch=auth.epoch,
                message=message,
                writer_sig=auth.sig,
                suite=auth.sig_suite,
            )
            return household
        raise PermissionError("unknown upload authorization")  # pragma: no cover

    async def _authorize_delete(
        self,
        scope_kind: str,
        scope_id: str,
        blob_id: str,
        *,
        ts: object,
        sig: object,
        sig_suite: object,
    ) -> None:
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
        # Checked after the signature verified, so a replay costs what a
        # success does (one verification) and an unverified body can't
        # poison the cache. Keyed on the signed payload alone (scope, blob,
        # ts) — never on the signature's text, which another encoding of
        # the same bytes could vary.
        seen = self._deletes_seen.digest(payload)
        if self._deletes_seen.seen(seen):
            raise PermissionError("replayed delete")
        self._deletes_seen.record(seen)

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


def _active(space: GlobalSpace) -> GlobalSpace:
    """Only an ACTIVE listed space takes media — not a pending one a
    household published but a moderator hasn't accepted (third review A3)."""
    if space.status != "active":
        raise PermissionError("space is not active")
    return space


def _burn_verifies(n: int) -> None:
    """Make *n* throw-away Ed25519 verifications against the fixed dummy key
    the other early-refusal paths use; the result is discarded."""
    key = bytes.fromhex(TIMING_UNIFORM_DUMMY_KEY_HEX)
    for _ in range(n):
        verify_ed25519(key, _BURN_MESSAGE, TIMING_UNIFORM_DUMMY_SIG)


#: The routed writes, by aiohttp canonical path → method. One scope-free
#: path: the scope rides only inside the signed claim / delete body, never
#: in a URL an access log or a proxy keeps.
SPACE_MEDIA_WRITE_ROUTES: dict[str, str] = {
    "/gfs/media/{blob_id}": "PUT",
    "/gfs/media/{blob_id}/delete": "POST",
}


def is_space_media_write(method: str, canonical: str | None) -> bool:
    """An upload or a delete as ROUTED — matched on the route the request
    resolved to, never on the raw path, so no spelling of the path (an
    encoded slash, a doubled one) slips past the limiter. Not the public
    ``GET`` of the same path (a CDN serves most of those)."""
    return canonical is not None and SPACE_MEDIA_WRITE_ROUTES.get(canonical) == method


def build_space_media_rate_limit(
    resolver: ClientIpResolver, *, nodes: Callable[[], int] | None = None
):
    """Per-IP limiter for the space media uploads and deletes. With *nodes*
    (the cluster's live node count) it is cluster-wide: each node allows
    its :func:`~.public.node_share` of the budget."""
    counter = SlidingWindowCounter(SPACE_MEDIA_MAX_PER_MINUTE_PER_IP)

    @web.middleware
    async def _rate_limit(request: web.Request, handler):
        route = request.match_info.route
        resource = route.resource if route is not None else None
        canonical = resource.canonical if resource is not None else None
        if not is_space_media_write(request.method, canonical):
            return await handler(request)
        limit = SPACE_MEDIA_MAX_PER_MINUTE_PER_IP
        share = limit if nodes is None else node_share(limit, nodes())
        if not counter.allow(resolver.bucket(request), limit=share):
            resp = web.json_response({"error": "rate_limited"}, status=429)
            resp.headers["Retry-After"] = "60"
            return resp
        return await handler(request)

    return _rate_limit
