"""Gallery routes — albums + items (section 23.119).

Endpoints:

* ``GET    /api/spaces/{space_id}/gallery/albums``      — list albums
* ``POST   /api/spaces/{space_id}/gallery/albums``      — create album
* ``GET    /api/gallery/albums/{album_id}``             — album detail
* ``PATCH  /api/gallery/albums/{album_id}``             — update metadata
* ``DELETE /api/gallery/albums/{album_id}``             — delete (owner/admin)
* ``POST   /api/gallery/albums/{album_id}/retention``   — set retention_exempt
* ``GET    /api/gallery/albums/{album_id}/items``       — list items
* ``POST   /api/gallery/albums/{album_id}/items``       — upload item
* ``DELETE /api/gallery/items/{item_id}``               — delete item

Household-level albums (no parent space) live under
``/api/gallery/albums`` directly via the ``space_id=None`` listing.
"""

from __future__ import annotations

from aiohttp import web
from aiohttp.multipart import BodyPartReader

from .. import app_keys as K
from ..media_signer import sign_media_urls_in
from .base import BaseView


def _album_dict(a) -> dict:
    return {
        "id": a.id,
        "space_id": a.space_id,
        "owner_user_id": a.owner_user_id,
        "name": a.name,
        "description": a.description,
        "cover_item_id": a.cover_item_id,
        "cover_url": a.cover_url,
        "item_count": a.item_count,
        "retention_exempt": a.retention_exempt,
        "created_at": a.created_at,
        "updated_at": a.updated_at,
    }


def _item_dict(i) -> dict:
    return {
        "id": i.id,
        "album_id": i.album_id,
        "uploaded_by": i.uploaded_by,
        "item_type": i.item_type,
        "url": i.url,
        "thumbnail_url": i.thumbnail_url,
        "width": i.width,
        "height": i.height,
        "duration_s": i.duration_s,
        "caption": i.caption,
        "taken_at": i.taken_at,
        "sort_order": i.sort_order,
        "created_at": i.created_at,
    }


def _album_signed(request: web.Request, a) -> dict:
    """:func:`_album_dict` + sign ``cover_url`` for the SPA."""
    payload = _album_dict(a)
    signer = request.app.get(K.media_signer_key)
    if signer is not None:
        sign_media_urls_in(payload, signer)
    return payload


def _item_signed(request: web.Request, i) -> dict:
    """:func:`_item_dict` + sign ``url`` and ``thumbnail_url``. Gallery
    items expose the full media URL on the generic ``url`` field, so
    we opt in via the signer's ``extra_fields``."""
    payload = _item_dict(i)
    signer = request.app.get(K.media_signer_key)
    if signer is not None:
        sign_media_urls_in(payload, signer, extra_fields=("url",))
    return payload


class HouseholdAlbumCollectionView(BaseView):
    """GET/POST /api/gallery/albums — household-level albums."""

    async def get(self) -> web.Response:
        ctx = self.user
        before = self.request.query.get("before")
        try:
            limit = int(self.request.query.get("limit", 30))
        except ValueError:
            limit = 30
        albums = await self.svc(K.gallery_service_key).list_albums(
            space_id=None,
            actor_user_id=ctx.user_id,
            limit=limit,
            before=before,
        )
        return web.json_response([_album_signed(self.request, a) for a in albums])

    async def post(self) -> web.Response:
        ctx = self.user
        body = await self.body()
        album = await self.svc(K.gallery_service_key).create_album(
            space_id=None,
            owner_user_id=ctx.user_id,
            name=str(body.get("name", "")),
            description=body.get("description"),
        )
        return web.json_response(_album_signed(self.request, album), status=201)


class SpaceAlbumCollectionView(BaseView):
    """GET/POST /api/spaces/{space_id}/gallery/albums — space-scoped albums."""

    async def get(self) -> web.Response:
        ctx = self.user
        space_id = self.match("space_id")
        before = self.request.query.get("before")
        try:
            limit = int(self.request.query.get("limit", 30))
        except ValueError:
            limit = 30
        albums = await self.svc(K.gallery_service_key).list_albums(
            space_id=space_id,
            actor_user_id=ctx.user_id,
            limit=limit,
            before=before,
        )
        return web.json_response([_album_signed(self.request, a) for a in albums])

    async def post(self) -> web.Response:
        ctx = self.user
        space_id = self.match("space_id")
        body = await self.body()
        album = await self.svc(K.gallery_service_key).create_album(
            space_id=space_id,
            owner_user_id=ctx.user_id,
            name=str(body.get("name", "")),
            description=body.get("description"),
        )
        return web.json_response(_album_signed(self.request, album), status=201)


class AlbumDetailView(BaseView):
    """GET/PATCH/DELETE /api/gallery/albums/{album_id}."""

    async def get(self) -> web.Response:
        ctx = self.user
        album = await self.svc(K.gallery_service_key).get_album(
            self.match("album_id"),
            actor_user_id=ctx.user_id,
        )
        return web.json_response(_album_signed(self.request, album))

    async def patch(self) -> web.Response:
        ctx = self.user
        body = await self.body()
        await self.svc(K.gallery_service_key).update_album(
            self.match("album_id"),
            actor_user_id=ctx.user_id,
            name=body.get("name"),
            description=body.get("description"),
            cover_item_id=body.get("cover_item_id"),
        )
        return web.Response(status=204)

    async def delete(self) -> web.Response:
        ctx = self.user
        await self.svc(K.gallery_service_key).delete_album(
            self.match("album_id"),
            actor_user_id=ctx.user_id,
        )
        return web.Response(status=204)


class AlbumRetentionView(BaseView):
    """POST /api/gallery/albums/{album_id}/retention — set retention_exempt."""

    async def post(self) -> web.Response:
        ctx = self.user
        try:
            body = await self.body()
        except Exception:
            body = {}
        exempt = bool(body.get("retention_exempt"))
        await self.svc(K.gallery_service_key).set_retention_exempt(
            self.match("album_id"),
            exempt,
            actor_user_id=ctx.user_id,
        )
        return web.json_response({"retention_exempt": exempt})


class AlbumItemCollectionView(BaseView):
    """GET/POST /api/gallery/albums/{album_id}/items — list or upload items."""

    async def get(self) -> web.Response:
        ctx = self.user
        before = self.request.query.get("before")
        try:
            limit = int(self.request.query.get("limit", 50))
        except ValueError:
            limit = 50
        items = await self.svc(K.gallery_service_key).list_items(
            self.match("album_id"),
            actor_user_id=ctx.user_id,
            limit=limit,
            before=before,
        )
        return web.json_response(
            [_item_signed(self.request, i) for i in items],
        )

    async def post(self) -> web.Response:
        ctx = self.user
        album_id = self.match("album_id")
        caption = self.request.query.get("caption")

        # Hard cap on the request body — prevents OOM on a hostile client
        # trying to upload a 2 GB file. 100 MiB mirrors what the browser
        # guard enforces client-side.
        MAX_UPLOAD_BYTES = 100 * 1024 * 1024
        declared = self.request.content_length
        if declared is not None and declared > MAX_UPLOAD_BYTES:
            return web.json_response(
                {"error": "file_too_large", "limit_bytes": MAX_UPLOAD_BYTES},
                status=413,
            )

        # Accept multipart upload (preferred — frontend uses FormData)
        # or raw image bytes with a Content-Type header (CLI/scripts).
        content_type = self.request.headers.get("Content-Type", "")
        if content_type.startswith("multipart/"):
            try:
                reader = await self.request.multipart()
                field = await reader.next()
            except Exception:
                return web.json_response({"error": "bad_multipart"}, status=400)
            if field is None:
                return web.json_response({"error": "missing file"}, status=422)
            if not isinstance(field, BodyPartReader):
                return web.json_response({"error": "expected file part"}, status=400)
            data = await field.read(decode=False)
            content_type = field.headers.get("Content-Type", "image/jpeg")
        else:
            data = await self.request.read()
            if not content_type:
                content_type = "application/octet-stream"

        if len(data) > MAX_UPLOAD_BYTES:
            return web.json_response(
                {"error": "file_too_large", "limit_bytes": MAX_UPLOAD_BYTES},
                status=413,
            )

        item = await self.svc(K.gallery_service_key).upload_item(
            album_id,
            data=data,
            content_type=content_type,
            caption=caption,
            uploader_user_id=ctx.user_id,
        )
        return web.json_response(_item_signed(self.request, item), status=201)


class GalleryItemDetailView(BaseView):
    """DELETE /api/gallery/items/{item_id} — delete an item."""

    async def delete(self) -> web.Response:
        ctx = self.user
        await self.svc(K.gallery_service_key).delete_item(
            self.match("item_id"),
            actor_user_id=ctx.user_id,
        )
        return web.Response(status=204)
