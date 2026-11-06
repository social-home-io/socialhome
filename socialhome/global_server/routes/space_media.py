"""The space media cache routes (:mod:`..space_media`) — ONE scope-free
path. The scope (a listed space, or a PRIVATE space's opaque v_51 channel —
this server never learns that space) rides only inside what is signed: the
upload's claim header and the delete's body. So neither an access log, a
proxy nor a CDN cache key ever sees a space or channel id next to a blob
(the v_51 rule: a channel id never in a URL path).

* ``PUT /gfs/media/{blob_id}`` — the raw blob is the body; the authorization
  rides in the ``X-SH-Space-Media-Auth`` header
  (:mod:`socialhome.domain.gfs_space_media`) and is checked BEFORE the body
  is read; then the size must be a padded bucket and the scope's upload slot
  is reserved — all before the body is read, up to exactly the signed
  ``size`` (413 past it), making progress in every window
  (:data:`PROGRESS_WINDOW_S`, ``408`` otherwise), while one client bucket
  has at most :data:`~..space_media.PER_CLIENT_BODY_READS` bodies in
  flight, one network prefix :data:`~..space_media.PER_PREFIX_BODY_READS`,
  and the node holds at most :data:`~..space_media.MAX_BODY_BYTES_IN_FLIGHT`
  buffered bytes (``503 busy`` past any). ``201
  {"stored": true}``; the scope's retry is ``200 {"stored": false,
  "status": "already_stored"}`` (never a conflict, and never a fresher
  expiry). ``503 space_media_disabled`` while the operator has the cache off.
* ``GET /gfs/media/{blob_id}`` — anyone fetches a blob by address:
  ciphertext only, no credentials, readable cross-origin. ``200`` is
  ``Cache-Control: public, max-age=3600, immutable``: short on purpose, so a
  deleted or banned post's picture lingers at most an hour in an edge cache
  (readable only by holders of the space key). A miss is ``404`` with
  ``no-store``, so a blob uploaded a moment later is not hidden behind a
  cached miss.
* ``POST /gfs/media/{blob_id}/delete`` — the scope's seed holder withdraws
  the blob: ``{scope, scope_id, ts, sig, sig_suite}`` (space authority /
  channel key, over the scope, the blob and ``ts``).

Every authorization failure (unknown / banned space, unknown or unused
channel, wrong key, stale ts, bad cert, a replayed delete…) is ONE ``403``
body — no oracle — with the reason at DEBUG.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from ...domain.gfs_space_media import (
    SCOPE_SPACE,
    SPACE_MEDIA_AUTH_HEADER,
    InvalidSpaceMediaAuth,
    SpaceMediaUploadAuth,
)
from .. import app_keys as K
from ..blob_store import MediaRefused
from .base import GfsBaseView

log = logging.getLogger(__name__)

_REFUSED = {"error": "not authorized for this scope"}

#: A blob as anyone may read it: anonymous, cross-origin, an hour in a CDN.
_MEDIA_HEADERS = {
    "Content-Type": "application/octet-stream",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "public, max-age=3600, immutable",
    "Access-Control-Allow-Origin": "*",
    "Cross-Origin-Resource-Policy": "cross-origin",
}
_MISS_HEADERS = {
    "Cache-Control": "no-store",
    "Access-Control-Allow-Origin": "*",
}

#: Body cap of a delete (three short fields).
_MAX_DELETE_BYTES = 2048

#: Answer to an upload-rate refusal: the window is an hour.
_QUOTA_RETRY_AFTER_S = "600"

#: Answer while this client (or the node) reads as many bodies as it may.
_BUSY_RETRY_AFTER_S = "5"

#: Progress, not one long deadline: an upload body must deliver at least
#: :data:`MIN_PROGRESS_BYTES` (or the rest of it) in every window of
#: :data:`PROGRESS_WINDOW_S` — a client trickling or stalling is ``408`` and
#: its slot given back (third review A5).
PROGRESS_WINDOW_S: float = 5.0
MIN_PROGRESS_BYTES: int = 64 * 1024


def _refused() -> web.Response:
    return web.json_response(_REFUSED, status=403)


def _media_refused(exc: MediaRefused) -> web.Response:
    resp = web.json_response({"error": exc.reason}, status=exc.status)
    if exc.status == 429:
        resp.headers["Retry-After"] = _QUOTA_RETRY_AFTER_S
    return resp


#: The routes (aiohttp patterns, the address constrained).
SPACE_MEDIA_ROUTE: str = "/gfs/media/{blob_id:[0-9a-f]{64}}"
SPACE_MEDIA_DELETE_ROUTE: str = "/gfs/media/{blob_id:[0-9a-f]{64}}/delete"


class GfsMediaView(GfsBaseView):
    """``GET`` (public, by address) and ``PUT`` (an authorized upload) of
    ``/gfs/media/{blob_id}``."""

    async def get(self) -> web.Response:
        data = await self.svc(K.gfs_space_media_store_key).read(self.match("blob_id"))
        if data is None:
            return web.Response(status=404, headers=_MISS_HEADERS)
        return web.Response(body=data, headers=_MEDIA_HEADERS)

    async def read_body(self, size: int) -> bytes:
        """The upload body, up to *size* bytes, making progress — its bytes
        counted in the node's buffered total while the read holds them."""
        gate = self.svc(K.gfs_space_media_store_key).body_gate
        held = [0]

        def arrived(n: int) -> None:
            held[0] += n
            gate.buffered(n)

        try:
            return await _read_with_progress(self.request, size, on_bytes=arrived)
        finally:
            gate.release_buffer(held[0])

    async def put(self) -> web.Response:
        blob_id = self.match("blob_id")
        store = self.svc(K.gfs_space_media_store_key)
        try:
            # The operator turned the cache off: a clear 503 before any work.
            store.require_enabled()
        except MediaRefused as exc:
            return _media_refused(exc)
        try:
            auth = SpaceMediaUploadAuth.from_header(
                self.request.headers.get(SPACE_MEDIA_AUTH_HEADER, "")
            )
        except InvalidSpaceMediaAuth as exc:
            raise web.HTTPBadRequest(
                reason=f"Invalid {SPACE_MEDIA_AUTH_HEADER}"
            ) from exc
        claim = auth.claim
        try:
            household = await self.svc(K.gfs_space_media_auth_key).authorize_upload(
                auth, scope_kind=claim.scope, scope_id=claim.scope_id, blob_id=blob_id
            )
        except PermissionError as exc:
            log.debug("space media upload refused: %s", exc)
            return _refused()
        size = claim.size
        # The fairness owner: a space's owning household; a channel's blob
        # counts against its UPLOADER's address bucket (a keyed hash —
        # identity-free), never a seated member (fourth review G1).
        owner = (
            ("household", household)
            if claim.scope == SCOPE_SPACE
            else ("client", store.client_key(self.client_bucket()))
        )
        try:
            # Size bucket, caps, the scope's rate slot and the address's
            # byte budget — before any body.
            reserved = await store.admit_upload(
                scope_kind=claim.scope,
                scope_id=claim.scope_id,
                blob_id=blob_id,
                size=size,
                client=self.client_bucket(),
            )
        except MediaRefused as exc:
            return _media_refused(exc)
        if reserved is None:
            # The scope already holds it: answered without reading the body.
            return web.json_response({"stored": False, "status": "already_stored"})
        client_key = store.client_key(self.client_bucket())
        prefix_key = store.prefix_key(self.client_ip())
        if not store.body_gate.enter(client_key, prefix_key):
            # This client already has its share of bodies in flight (or the
            # node its bytes): answer now rather than queue another buffer
            # (security re-attack N5, third review A5).
            await store.release_upload(reserved)
            resp = web.json_response({"error": "busy"}, status=503)
            resp.headers["Retry-After"] = _BUSY_RETRY_AFTER_S
            return resp
        try:
            data = await self.read_body(size)
        except TimeoutError:
            await store.release_upload(reserved)
            return web.json_response({"error": "body_timeout"}, status=408)
        except BaseException:
            await store.release_upload(reserved)
            raise
        finally:
            store.body_gate.leave(client_key, prefix_key)
        if len(data) != size:
            await store.release_upload(reserved)
            return web.json_response({"error": "size_mismatch"}, status=422)
        try:
            stored = await store.put(
                scope_kind=claim.scope,
                scope_id=claim.scope_id,
                blob_id=blob_id,
                data=data,
                expires_at=claim.expires_at,
                reserved=reserved,
                owner=owner,
            )
        except MediaRefused as exc:
            return _media_refused(exc)
        if stored:
            return web.json_response({"stored": True}, status=201)
        return web.json_response({"stored": False, "status": "already_stored"})


async def _read_with_progress(
    request: web.Request, size: int, *, on_bytes=None
) -> bytes:
    """Read a body of at most *size* bytes, requiring progress: at least
    :data:`MIN_PROGRESS_BYTES` (or the rest) in every
    :data:`PROGRESS_WINDOW_S` window — :class:`TimeoutError` otherwise; a
    body past *size* is ``413``."""
    declared = request.content_length
    if declared is not None and declared > size:
        raise web.HTTPRequestEntityTooLarge(max_size=size, actual_size=declared)
    loop = asyncio.get_running_loop()
    buf = bytearray()
    window_end = loop.time() + PROGRESS_WINDOW_S
    window_start_len = 0
    while True:
        need = min(MIN_PROGRESS_BYTES, size - window_start_len)
        try:
            chunk = await asyncio.wait_for(
                request.content.readany(), max(window_end - loop.time(), 0.001)
            )
        except TimeoutError:
            if len(buf) - window_start_len < need:
                raise
            window_end = loop.time() + PROGRESS_WINDOW_S
            window_start_len = len(buf)
            continue
        if not chunk:
            return bytes(buf)
        buf += chunk
        if on_bytes is not None:
            on_bytes(len(chunk))
        if len(buf) > size:
            raise web.HTTPRequestEntityTooLarge(max_size=size, actual_size=len(buf))
        if loop.time() >= window_end:
            if len(buf) - window_start_len < need:
                raise TimeoutError
            window_end = loop.time() + PROGRESS_WINDOW_S
            window_start_len = len(buf)


class GfsMediaDeleteView(GfsBaseView):
    """``POST /gfs/media/{blob_id}/delete`` — the scope's seed holder
    withdraws a blob; the scope only in the signed body.
    ``{"deleted": bool}``."""

    async def post(self) -> web.Response:
        blob_id = self.match("blob_id")
        body = await self.bounded_json(_MAX_DELETE_BYTES)
        scope_kind, scope_id = body.get("scope"), body.get("scope_id")
        try:
            if not isinstance(scope_kind, str) or not isinstance(scope_id, str):
                raise PermissionError("delete names no scope")
            await self.svc(K.gfs_space_media_auth_key).authorize_delete(
                scope_kind,
                scope_id,
                blob_id,
                ts=body.get("ts"),
                sig=body.get("sig"),
                sig_suite=body.get("sig_suite"),
            )
        except PermissionError as exc:
            log.debug("space media delete refused: %s", exc)
            return _refused()
        deleted = await self.svc(K.gfs_space_media_store_key).delete(
            blob_id, scope_kind=scope_kind, scope_id=scope_id
        )
        return web.json_response({"deleted": deleted})
