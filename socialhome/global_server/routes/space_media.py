"""The space media cache routes (:mod:`..space_media`).

Two upload scopes, one public fetch:

* ``PUT /gfs/spaces/{space_id}/media/{blob_id}`` — a listed space;
* ``PUT /gfs/channels/{channel_id}/media/{blob_id}`` — a PRIVATE space's
  opaque v_51 channel (this server never learns the space).

  The raw blob is the body; the authorization rides in the
  ``X-SH-Space-Media-Auth`` header (:mod:`socialhome.domain.gfs_space_media`)
  and is checked BEFORE the body is read, which is then read up to exactly
  the signed ``size`` (413 past it). ``201 {"stored": true}``; a re-upload of
  a held blob is ``200 {"stored": false, "status": "already_stored"}``.
* ``GET /gfs/media/{blob_id}`` — anyone fetches a blob by address:
  ciphertext only, no credentials, readable cross-origin. The path names no
  scope, so neither an access log nor a CDN cache key tells which space or
  channel a reader reads (and a channel's blob is never marked "space") —
  the address is unique on its own. ``200`` is
  ``Cache-Control: public, max-age=3600, immutable``: short on purpose, so a
  deleted post's picture lingers at most an hour in an edge cache (readable
  only by holders of the space key). A miss is ``404`` with ``no-store``, so
  a blob uploaded a moment later is not hidden behind a cached miss.
* ``POST …/media/{blob_id}/delete`` (both scopes) — the scope's seed holder
  withdraws a blob: ``{ts, sig, sig_suite}`` (space authority / channel key).

Every authorization failure (unknown / banned space, unknown channel, wrong
key, stale ts, bad cert…) is ONE ``403`` body — no oracle — with the reason
at DEBUG.
"""

from __future__ import annotations

import logging

from aiohttp import web

from ...domain.gfs_space_media import (
    SCOPE_CHANNEL,
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


def _refused() -> web.Response:
    return web.json_response(_REFUSED, status=403)


def _media_refused(exc: MediaRefused) -> web.Response:
    resp = web.json_response({"error": exc.reason}, status=exc.status)
    if exc.status == 429:
        resp.headers["Retry-After"] = _QUOTA_RETRY_AFTER_S
    return resp


class _ScopedMediaView(GfsBaseView):
    """Upload + delete for one scope; subclasses name it."""

    scope_kind: str = ""
    id_param: str = ""

    def _scope_id(self) -> str:
        return self.match(self.id_param)

    async def put(self) -> web.Response:
        scope_id = self._scope_id()
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
                auth, scope_kind=self.scope_kind, scope_id=scope_id, blob_id=blob_id
            )
        except PermissionError as exc:
            log.debug(
                "space media upload refused for %s %s: %s",
                self.scope_kind,
                scope_id,
                exc,
            )
            return _refused()
        store = self.svc(K.gfs_space_media_store_key)
        size = auth.claim.size
        try:
            store.admit_size(size)
            data = await self.bounded_raw(size)
            if len(data) != size:
                return web.json_response({"error": "size_mismatch"}, status=422)
            stored = await store.put(
                scope_kind=self.scope_kind,
                scope_id=scope_id,
                blob_id=blob_id,
                data=data,
                expires_at=auth.claim.expires_at,
            )
        except MediaRefused as exc:
            return _media_refused(exc)
        if stored:
            return web.json_response({"stored": True}, status=201)
        return web.json_response({"stored": False, "status": "already_stored"})


class _ScopedMediaDeleteView(GfsBaseView):
    """``POST …/media/{blob_id}/delete`` — the scope's seed holder withdraws
    a blob. ``{"deleted": bool}``."""

    scope_kind: str = ""
    id_param: str = ""

    async def post(self) -> web.Response:
        scope_id = self.match(self.id_param)
        blob_id = self.match("blob_id")
        body = await self.bounded_json(_MAX_DELETE_BYTES)
        try:
            await self.svc(K.gfs_space_media_auth_key).authorize_delete(
                self.scope_kind,
                scope_id,
                blob_id,
                ts=body.get("ts"),
                sig=body.get("sig"),
                sig_suite=body.get("sig_suite"),
            )
        except PermissionError as exc:
            log.debug(
                "space media delete refused for %s %s: %s",
                self.scope_kind,
                scope_id,
                exc,
            )
            return _refused()
        deleted = await self.svc(K.gfs_space_media_store_key).delete(
            blob_id, scope_kind=self.scope_kind, scope_id=scope_id
        )
        return web.json_response({"deleted": deleted})


class GfsSpaceMediaUploadView(_ScopedMediaView):
    """``PUT /gfs/spaces/{space_id}/media/{blob_id}``."""

    scope_kind = SCOPE_SPACE
    id_param = "space_id"


class GfsChannelMediaUploadView(_ScopedMediaView):
    """``PUT /gfs/channels/{channel_id}/media/{blob_id}``."""

    scope_kind = SCOPE_CHANNEL
    id_param = "channel_id"


class GfsSpaceMediaDeleteView(_ScopedMediaDeleteView):
    """``POST /gfs/spaces/{space_id}/media/{blob_id}/delete``."""

    scope_kind = SCOPE_SPACE
    id_param = "space_id"


class GfsChannelMediaDeleteView(_ScopedMediaDeleteView):
    """``POST /gfs/channels/{channel_id}/media/{blob_id}/delete``."""

    scope_kind = SCOPE_CHANNEL
    id_param = "channel_id"


class GfsMediaView(GfsBaseView):
    """``GET /gfs/media/{blob_id}`` — public, by address, any scope."""

    async def get(self) -> web.Response:
        data = await self.svc(K.gfs_space_media_store_key).read(self.match("blob_id"))
        if data is None:
            return web.Response(status=404, headers=_MISS_HEADERS)
        return web.Response(body=data, headers=_MEDIA_HEADERS)
