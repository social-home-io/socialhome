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
  ``size`` (413 past it) within :data:`BODY_READ_TIMEOUT_S`. ``201
  {"stored": true}``; the same bytes again — a retry, or another scope's
  upload, which adds that scope's claim — are ``200 {"stored": false,
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

import logging

from aiohttp import web

from ...domain.gfs_space_media import (
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
            await self.svc(K.gfs_space_media_auth_key).authorize_upload(
                auth, scope_kind=claim.scope, scope_id=claim.scope_id, blob_id=blob_id
            )
        except PermissionError as exc:
            log.debug("space media upload refused: %s", exc)
            return _refused()
        size = claim.size
        try:
            store.admit_size(size)
            data = await self.bounded_raw(size)
            if len(data) != size:
                return web.json_response({"error": "size_mismatch"}, status=422)
            stored = await store.put(
                scope_kind=claim.scope,
                scope_id=claim.scope_id,
                blob_id=blob_id,
                data=data,
                expires_at=claim.expires_at,
            )
        except MediaRefused as exc:
            return _media_refused(exc)
        if stored:
            return web.json_response({"stored": True}, status=201)
        return web.json_response({"stored": False, "status": "already_stored"})


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
