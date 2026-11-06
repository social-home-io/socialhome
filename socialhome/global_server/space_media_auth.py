"""Who may put a blob into — or take one out of — the space media cache.

Identity-free by design: nothing here looks up which household is asking.
An upload is authorized by exactly what this server already verifies for a
space's other traffic (:mod:`socialhome.domain.gfs_space_media` has the wire
shape):

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
The claim's ``ts`` must be within :data:`SPACE_MEDIA_TS_SKEW_S` of this
server's clock. A captured upload replayed inside that window is harmless:
the blob is content-addressed, so it is the same bytes again (a no-op), or
lands on another GFS listing the same space, inside that space's quota there.

A delete is the space authority's alone (``{space_id, blob_id, ts}`` under
``space_media_delete``). Every refusal is a :class:`PermissionError`; the
route answers all of them with one uniform 403.
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
    AUTH_WRITER_CERT,
    AUTH_WRITER_KEY,
    SpaceMediaUploadAuth,
    UnsupportedSpaceMediaSigSuite,
    require_space_media_sig_suite,
    space_media_delete_payload,
)

if TYPE_CHECKING:
    from .federation import GfsFederationService
    from .member_publish import GfsMemberPublishService

#: How far an upload's or delete's ``ts`` may be from this server's clock —
#: the window of every signed household request.
SPACE_MEDIA_TS_SKEW_S: int = 300


class GfsSpaceMediaAuth:
    """Authorize uploads to and deletes from the space media cache."""

    __slots__ = ("_clock", "_federation", "_member_publish")

    def __init__(
        self,
        *,
        federation: GfsFederationService,
        member_publish: GfsMemberPublishService,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._federation = federation
        self._member_publish = member_publish
        self._clock = clock

    async def authorize_upload(
        self, auth: SpaceMediaUploadAuth, *, space_id: str, blob_id: str
    ) -> None:
        """Raise :class:`PermissionError` unless *auth* lets its holder put
        *blob_id* into *space_id* (the path's values — the signed claim must
        name the same)."""
        claim = auth.claim
        if claim.space_id != space_id or claim.blob_id != blob_id:
            raise PermissionError("claim names another space or blob")
        self._require_fresh(claim.ts)
        if auth.kind == AUTH_AUTHORITY:
            await self._federation.verify_space_authority(
                space_id,
                event_type=AUTHORITY_EVENT_SPACE_MEDIA_PUT,
                payload=claim.signing_body(),
                authority_sig=auth.sig,
                authority_sig_suite=auth.sig_suite,
            )
            return
        space = await self._federation.listed_space(space_id)
        if auth.epoch is None:  # pragma: no cover - the codec requires one
            raise PermissionError("writer upload without an epoch")
        message = claim.writer_signing_bytes(kind=auth.kind, epoch=auth.epoch)
        if auth.kind == AUTH_WRITER_CERT and auth.writer_cert is not None:
            try:
                require_space_media_sig_suite(auth.sig_suite)
                author_pk = b64url_decode(auth.writer_cert.instance_pk)
                ok = verify_ed25519(author_pk, message, b64url_decode(auth.sig))
            except (UnsupportedSpaceMediaSigSuite, ValueError, TypeError) as exc:
                raise PermissionError(f"writer signature refused: {exc}") from exc
            if not ok:
                raise PermissionError("writer signature does not verify")
            await self._member_publish.verify_writer_cert(
                space, auth.writer_cert, epoch=auth.epoch, author_pk=author_pk
            )
            return
        if auth.kind == AUTH_WRITER_KEY:
            await self._member_publish.verify_writer_key_sig(
                space_id,
                epoch=auth.epoch,
                message=message,
                writer_sig=auth.sig,
                suite=auth.sig_suite,
            )
            await self._member_publish.require_writer_key_epoch(space_id, auth.epoch)
            return
        raise PermissionError("unknown upload authorization")  # pragma: no cover

    async def authorize_delete(
        self,
        space_id: str,
        blob_id: str,
        *,
        ts: object,
        authority_sig: object,
        authority_sig_suite: object,
    ) -> None:
        """Raise :class:`PermissionError` unless a seed holder of *space_id*
        signed this delete of *blob_id* within the skew window."""
        if not isinstance(ts, int) or isinstance(ts, bool):
            raise PermissionError("bad ts")
        self._require_fresh(ts)
        if not isinstance(authority_sig, str) or not isinstance(
            authority_sig_suite, str
        ):
            raise PermissionError("bad authority signature")
        await self._federation.verify_space_authority(
            space_id,
            event_type=AUTHORITY_EVENT_SPACE_MEDIA_DELETE,
            payload=space_media_delete_payload(space_id, blob_id, ts),
            authority_sig=authority_sig,
            authority_sig_suite=authority_sig_suite,
        )

    def _require_fresh(self, ts: int) -> None:
        if abs(ts - self._clock()) > SPACE_MEDIA_TS_SKEW_S:
            raise PermissionError("stale or future timestamp")
