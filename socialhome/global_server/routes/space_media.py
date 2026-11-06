"""The space media cache routes (:mod:`..space_media`).

* ``PUT /gfs/spaces/{space_id}/media/{blob_id}`` — a household uploads one
  encrypted blob. The raw blob is the body; the authorization rides in the
  ``X-SH-Space-Media-Auth`` header (:mod:`socialhome.domain.gfs_space_media`)
  and is checked BEFORE the body is read, which is then read up to exactly
  the signed ``size`` (413 past it). ``201 {"stored": true}``; a re-upload of
  a held blob is ``200 {"stored": false, "status": "already_stored"}``.
* ``GET /gfs/spaces/media/{blob_id}`` — anyone fetches a blob by address:
  ciphertext only, no credentials, readable cross-origin. The path names no
  space, so neither an access log nor a CDN cache key tells which space a
  reader reads — the address is unique on its own. ``200`` is
  ``Cache-Control: public, max-age=3600, immutable``: short on purpose, so a
  deleted post's picture lingers at most an hour in an edge cache (readable
  only by holders of the space key). A miss is ``404`` with ``no-store``, so
  a blob uploaded a moment later is not hidden behind a cached miss.
* ``POST /gfs/spaces/{space_id}/media/{blob_id}/delete`` — the space
  authority withdraws a blob: ``{ts, authority_sig, authority_sig_suite}``.

Every authorization failure (unknown / banned space, wrong key, stale ts,
bad cert…) is ONE ``403`` body — no oracle — with the reason at DEBUG.
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

_REFUSED = {"error": "not authorized for this space"}

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

#: Answer to a per-space quota refusal: the upload window is an hour.
_QUOTA_RETRY_AFTER_S = "600"


def _refused() -> web.Response:
    return web.json_response(_REFUSED, status=403)


def _media_refused(exc: MediaRefused) -> web.Response:
    resp = web.json_response({"error": exc.reason}, status=exc.status)
    if exc.status == 429:
        resp.headers["Retry-After"] = _QUOTA_RETRY_AFTER_S
    return resp


class GfsSpaceMediaUploadView(GfsBaseView):
    """``PUT /gfs/spaces/{space_id}/media/{blob_id}``."""

    async def put(self) -> web.Response:
        space_id = self.match("space_id")
        blob_id = self.match("blob_id")
        try:
            auth = SpaceMediaUploadAuth.from_header(
                self.request.headers.get(SPACE_MEDIA_AUTH_HEADER, "")
            )
        except InvalidSpaceMediaAuth as exc:
            raise web.HTTPBadRequest(
                reason=f"Invalid {SPACE_MEDIA_AUTH_HEADER}"
            ) from exc
        try:
            await self.svc(K.gfs_space_media_auth_key).authorize_upload(
                auth, space_id=space_id, blob_id=blob_id
            )
        except PermissionError as exc:
            log.debug("space media upload refused for space %s: %s", space_id, exc)
            return _refused()
        store = self.svc(K.gfs_space_media_store_key)
        size = auth.claim.size
        if size > store.limits.max_blob_bytes:
            return _media_refused(MediaRefused(413, "too_large_for_cache"))
        data = await self.bounded_raw(size)
        if len(data) != size:
            return web.json_response({"error": "size_mismatch"}, status=422)
        try:
            stored = await store.put(
                space_id=space_id,
                blob_id=blob_id,
                data=data,
                expires_at=auth.claim.expires_at,
            )
        except MediaRefused as exc:
            return _media_refused(exc)
        if stored:
            return web.json_response({"stored": True}, status=201)
        return web.json_response({"stored": False, "status": "already_stored"})


class GfsSpaceMediaView(GfsBaseView):
    """``GET /gfs/spaces/media/{blob_id}`` — public, by address."""

    async def get(self) -> web.Response:
        data = await self.svc(K.gfs_space_media_store_key).read(self.match("blob_id"))
        if data is None:
            return web.Response(status=404, headers=_MISS_HEADERS)
        return web.Response(body=data, headers=_MEDIA_HEADERS)


class GfsSpaceMediaDeleteView(GfsBaseView):
    """``POST /gfs/spaces/{space_id}/media/{blob_id}/delete`` — the space
    authority withdraws a blob. ``{"deleted": bool}``."""

    async def post(self) -> web.Response:
        space_id = self.match("space_id")
        blob_id = self.match("blob_id")
        body = await self.bounded_json(_MAX_DELETE_BYTES)
        try:
            await self.svc(K.gfs_space_media_auth_key).authorize_delete(
                space_id,
                blob_id,
                ts=body.get("ts"),
                authority_sig=body.get("authority_sig"),
                authority_sig_suite=body.get("authority_sig_suite"),
            )
        except PermissionError as exc:
            log.debug("space media delete refused for space %s: %s", space_id, exc)
            return _refused()
        deleted = await self.svc(K.gfs_space_media_store_key).delete(
            blob_id, space_id=space_id
        )
        return web.json_response({"deleted": deleted})
