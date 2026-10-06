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
from ..domain.errors import PayloadTooLargeError
from ..hardening import read_body_capped, read_part_capped
from ..media_signer import sign_media_urls_in
from .base import BaseView
from .media_status import READY, media_filename

_GALLERY_SECTION = "gallery"

#: Hard cap on one uploaded item — prevents OOM on a hostile client trying
#: to upload a 2 GB file. 100 MiB mirrors what the browser guard enforces
#: client-side. Enforced while streaming (``read_*_capped``), so it holds
#: for chunked bodies too and is independent of aiohttp's 1 MiB
#: ``client_max_size``.
GALLERY_MAX_UPLOAD_BYTES: int = 100 * 1024 * 1024


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
        # The SPA renders system albums (auto-mirrored "Posts") with a
        # different visual treatment + the "Auto" badge.  Surface the
        # flag explicitly rather than asking the SPA to infer it from
        # ``owner_user_id == null`` — the inference is fragile if a
        # future album type ever has a null owner for a different
        # reason.
        "is_system": a.is_system,
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
        await self.svc(K.preferences_service_key).require_enabled(_GALLERY_SECTION)
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
        await self.svc(K.preferences_service_key).require_enabled(_GALLERY_SECTION)
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
        await self.svc(K.preferences_service_key).require_enabled(_GALLERY_SECTION)
        space_id = self.match("space_id")
        await self.require_space_feature(space_id, "gallery")
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
        await self.svc(K.preferences_service_key).require_enabled(_GALLERY_SECTION)
        space_id = self.match("space_id")
        await self.require_space_feature(space_id, "gallery")
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
        await self.svc(K.preferences_service_key).require_enabled(_GALLERY_SECTION)
        album = await self.svc(K.gallery_service_key).get_album(
            self.match("album_id"),
            actor_user_id=ctx.user_id,
        )
        return web.json_response(_album_signed(self.request, album))

    async def patch(self) -> web.Response:
        ctx = self.user
        await self.svc(K.preferences_service_key).require_enabled(_GALLERY_SECTION)
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
        await self.svc(K.preferences_service_key).require_enabled(_GALLERY_SECTION)
        await self.svc(K.gallery_service_key).delete_album(
            self.match("album_id"),
            actor_user_id=ctx.user_id,
        )
        return web.Response(status=204)


class AlbumRetentionView(BaseView):
    """POST /api/gallery/albums/{album_id}/retention — set retention_exempt."""

    async def post(self) -> web.Response:
        ctx = self.user
        await self.svc(K.preferences_service_key).require_enabled(_GALLERY_SECTION)
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
        await self.svc(K.preferences_service_key).require_enabled(_GALLERY_SECTION)
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
        # Video items transcode in the background — surface a live
        # ``media_status`` so the SPA shows a "Processing…" placeholder
        # until the ``.webm`` exists. One batched repo read per request;
        # photos/other items get no status field.
        video_fns = [
            fn
            for i in items
            if i.item_type == "video" and (fn := media_filename(i.url)) is not None
        ]
        statuses = await self.svc(K.media_transcode_repo_key).status_for(video_fns)
        out = []
        for i in items:
            payload = _item_signed(self.request, i)
            if i.item_type == "video":
                fn = media_filename(i.url)
                payload["media_status"] = statuses.get(fn, READY) if fn else READY
            out.append(payload)
        return web.json_response(out)

    async def post(self) -> web.Response:
        ctx = self.user
        await self.svc(K.preferences_service_key).require_enabled(_GALLERY_SECTION)
        album_id = self.match("album_id")
        caption = self.request.query.get("caption")

        # A declared ``Content-Length`` over the cap is refused before the
        # body is touched — on both paths, so the multipart reader is never
        # even opened for it. (``read_body_capped`` repeats the check for
        # the raw path; it is cheap.)
        declared = self.request.content_length
        if declared is not None and declared > GALLERY_MAX_UPLOAD_BYTES:
            raise PayloadTooLargeError(GALLERY_MAX_UPLOAD_BYTES)

        # Accept multipart upload (preferred — frontend uses FormData)
        # or raw image bytes with a Content-Type header (CLI/scripts).
        # Both paths stream under ``GALLERY_MAX_UPLOAD_BYTES``; an over-cap
        # body raises ``PayloadTooLargeError`` (413) via ``_iter``.
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
            data = await read_part_capped(field, GALLERY_MAX_UPLOAD_BYTES)
            content_type = field.headers.get("Content-Type", "image/jpeg")
        else:
            data = await read_body_capped(self.request, GALLERY_MAX_UPLOAD_BYTES)
            if not content_type:
                content_type = "application/octet-stream"

        item = await self.svc(K.gallery_service_key).upload_item(
            album_id,
            data=data,
            content_type=content_type,
            caption=caption,
            uploader_user_id=ctx.user_id,
        )
        payload = _item_signed(self.request, item)
        # A freshly-uploaded video transcodes in the background — the
        # ``.webm`` + poster don't exist on disk yet. Tell the SPA to
        # render a "processing" placeholder; the LIST endpoint (T4)
        # derives the live status from the transcode repo on refetch.
        if item.item_type == "video":
            payload["media_status"] = "processing"
        return web.json_response(payload, status=201)


class GalleryItemDetailView(BaseView):
    """DELETE /api/gallery/items/{item_id} — delete an item."""

    async def delete(self) -> web.Response:
        ctx = self.user
        await self.svc(K.preferences_service_key).require_enabled(_GALLERY_SECTION)
        await self.svc(K.gallery_service_key).delete_item(
            self.match("item_id"),
            actor_user_id=ctx.user_id,
        )
        return web.Response(status=204)
