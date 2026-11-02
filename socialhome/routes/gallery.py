"""Gallery routes — albums + items (section 23.119).

Endpoints:

* ``GET    /api/gallery/albums``                        — list household albums
* ``POST   /api/gallery/albums``                        — create household album
* ``GET    /api/gallery/default-album``                 — the default album
* ``POST   /api/gallery/default-album``                 — find or make it
* ``GET    /api/spaces/{space_id}/gallery/albums``      — list albums
* ``POST   /api/spaces/{space_id}/gallery/albums``      — create album
* ``GET    /api/gallery/albums/{album_id}``             — album detail
* ``PATCH  /api/gallery/albums/{album_id}``             — update metadata
* ``DELETE /api/gallery/albums/{album_id}``             — delete (owner/admin)
* ``POST   /api/gallery/albums/{album_id}/retention``   — set retention_exempt
* ``GET    /api/gallery/albums/{album_id}/items``       — list items
  (``?sort=taken`` / ``taken_asc``: by the day taken)
* ``POST   /api/gallery/albums/{album_id}/items``       — upload item
* ``DELETE /api/gallery/items/{item_id}``               — delete item (a
  household item goes to the trash)
* ``POST   /api/gallery/items/{item_id}/restore``       — back from the trash
* ``GET    /api/gallery/trash``                         — the household trash
* ``DELETE /api/gallery/trash/{item_id}``               — delete for good
* ``GET    /api/gallery/on-this-day``                   — today's memory card
* ``PATCH  /api/gallery/on-this-day``                   — its per-person settings

A video upload is streamed straight to disk (never held in memory) and
checked there: a household album keeps videos up to 5 minutes and takes
files up to 2 GiB (``GALLERY_VIDEO_MAX_*`` in ``domain/gallery.py``); a
space album keeps the protocol's 60 s and 100 MiB. A video that is still
converting carries ``media_status: "processing"`` and ``media_eta_s``
(seconds until it is ready, a rough estimate).

Albums page by newest activity with ``?limit=&before=&before_id=`` (see
:func:`_page_query`); items newest upload first the same way, or by the
day taken with ``?sort=`` and the ``after_day`` / ``after`` / ``after_id``
cursor (:func:`_item_order`).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from aiohttp import web
from aiohttp.multipart import BodyPartReader

from .. import app_keys as K
from ..domain.errors import PayloadTooLargeError
from ..domain.gallery import (
    GALLERY_MAX_UPLOAD_BYTES,
    GALLERY_VIDEO_MAX_UPLOAD_BYTES,
    TRASH_RETENTION_DAYS,
    GalleryTrashItem,
    GalleryUpload,
    ItemCursor,
    ItemSort,
    OnThisDayCard,
    OnThisDaySettings,
    UploadOutcome,
    is_video_upload,
)
from ..hardening import (
    STREAM_CHUNK_BYTES,
    iter_part_chunks,
    read_body_capped,
    read_part_capped,
)
from ..media_signer import sign_media_urls_in
from ..services.gallery_on_this_day_service import resolve_today
from .base import BaseView
from .media_status import READY, media_filename

log = logging.getLogger(__name__)

_GALLERY_SECTION = "gallery"

#: ``(method, path, cap)`` for the generic body-size middleware: the item
#: upload may declare up to a household video's cap (plus the multipart
#: framing). The route enforces the exact limit for the album while it
#: streams (:meth:`GalleryService.upload_limits`).
GALLERY_LARGE_UPLOAD: tuple[str, re.Pattern[str], int] = (
    "POST",
    re.compile(r"/api/gallery/albums/[^/]+/items"),
    GALLERY_VIDEO_MAX_UPLOAD_BYTES + 1024 * 1024,
)

__all__ = ["GALLERY_LARGE_UPLOAD", "GALLERY_MAX_UPLOAD_BYTES"]

#: Default page sizes. A page shorter than the ``limit`` asked for is the
#: last one; the next page is ``?before=<created_at>&before_id=<id>`` of
#: the last row (for albums: the last regular album — the system album is
#: pinned to the first page only).
ALBUM_PAGE: int = 30
ITEM_PAGE: int = 50


def _page_query(
    request: web.Request, default_limit: int
) -> tuple[str | None, str | None, int]:
    """``(before, before_id, limit)`` from the query; a non-number limit
    is the default (the repo clamps the range)."""
    q = request.query
    try:
        limit = int(q.get("limit", default_limit))
    except ValueError:
        limit = default_limit
    return q.get("before") or None, q.get("before_id") or None, limit


def _item_order(request: web.Request) -> tuple[ItemSort, ItemCursor | None]:
    """``?sort=`` (``added`` — the default, newest upload first — ``taken``
    or ``taken_asc``) and, for a date sort, the next page's cursor: the last
    item's ``day``, ``created_at`` and ``id`` as ``after_day`` / ``after`` /
    ``after_id`` (all three, or none). Anything else is a ``ValueError``
    (422)."""
    q = request.query
    try:
        sort = ItemSort(q.get("sort") or ItemSort.ADDED)
    except ValueError:
        raise ValueError("sort must be added, taken or taken_asc") from None
    parts = (q.get("after_day"), q.get("after"), q.get("after_id"))
    if sort is ItemSort.ADDED or not any(parts):
        return sort, None
    day, at, item_id = parts
    if not (day and at and item_id):
        raise ValueError("after_day, after and after_id go together")
    return sort, ItemCursor(day=day, created_at=at, item_id=item_id)


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
        # The list's "newest activity" order and its ``before`` cursor.
        "activity_at": a.activity_at or a.created_at,
        # The household's default album; ``default_name`` while nobody
        # renamed it — the SPA then shows it in the viewer's own language.
        "is_default": a.is_default,
        "default_name": a.default_name,
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
        # The day the album sorts it by (taken, else uploaded) — the date
        # sort's ``after_day`` cursor.
        "day": i.day,
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
        before, before_id, limit = _page_query(self.request, ALBUM_PAGE)
        albums = await self.svc(K.gallery_service_key).list_albums(
            space_id=None,
            actor_user_id=ctx.user_id,
            limit=limit,
            before=before,
            before_id=before_id,
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


class DefaultAlbumView(BaseView):
    """GET/POST /api/gallery/default-album — where a household upload goes
    when nobody picked an album.

    GET answers ``{"album": {...}}`` or ``{"album": null}`` (none yet).
    POST ``{name}`` returns the default album, making it under *name* (the
    uploader's word for "Photos", 1–80 characters) when there is none:
    201 when made, 200 when it already existed."""

    async def get(self) -> web.Response:
        ctx = self.user
        await self.svc(K.preferences_service_key).require_enabled(_GALLERY_SECTION)
        album = await self.svc(K.gallery_service_key).get_default_album(
            actor_user_id=ctx.user_id
        )
        return web.json_response(
            {"album": _album_signed(self.request, album) if album else None}
        )

    async def post(self) -> web.Response:
        ctx = self.user
        await self.svc(K.preferences_service_key).require_enabled(_GALLERY_SECTION)
        body = await self.body()
        album, created = await self.svc(K.gallery_service_key).ensure_default_album(
            actor_user_id=ctx.user_id,
            name=str(body.get("name", "")),
        )
        return web.json_response(
            _album_signed(self.request, album), status=201 if created else 200
        )


class SpaceAlbumCollectionView(BaseView):
    """GET/POST /api/spaces/{space_id}/gallery/albums — space-scoped albums."""

    async def get(self) -> web.Response:
        ctx = self.user
        await self.svc(K.preferences_service_key).require_enabled(_GALLERY_SECTION)
        space_id = self.match("space_id")
        await self.require_space_feature(space_id, "gallery")
        before, before_id, limit = _page_query(self.request, ALBUM_PAGE)
        albums = await self.svc(K.gallery_service_key).list_albums(
            space_id=space_id,
            actor_user_id=ctx.user_id,
            limit=limit,
            before=before,
            before_id=before_id,
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
        except web.HTTPBadRequest:
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
        before, before_id, limit = _page_query(self.request, ITEM_PAGE)
        sort, after = _item_order(self.request)
        items = await self.svc(K.gallery_service_key).list_items(
            self.match("album_id"),
            actor_user_id=ctx.user_id,
            limit=limit,
            before=before,
            before_id=before_id,
            sort=sort,
            after=after,
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
        waiting = [fn for fn, st in statuses.items() if st == "processing"]
        etas = await self.svc(K.media_transcode_service_key).estimates(waiting)
        out = []
        for i in items:
            payload = _item_signed(self.request, i)
            if i.item_type == "video":
                fn = media_filename(i.url)
                payload["media_status"] = statuses.get(fn, READY) if fn else READY
                if fn in etas:
                    payload["media_eta_s"] = etas[fn]
            out.append(payload)
        return web.json_response(out)

    async def post(self) -> web.Response:
        ctx = self.user
        await self.svc(K.preferences_service_key).require_enabled(_GALLERY_SECTION)
        album_id = self.match("album_id")
        caption = self.request.query.get("caption")
        gallery = self.svc(K.gallery_service_key)

        # What this album takes from this person — asked before the body
        # is touched, so an album they can't add to, or a declared
        # ``Content-Length`` over the cap or over the free disk, is refused
        # before a 2 GB video is sent. (``read_body_capped`` repeats the
        # length check for the raw path; it is cheap.)
        declared = self.request.content_length
        limits = await gallery.upload_limits(
            album_id, uploader_user_id=ctx.user_id, declared_bytes=declared
        )
        largest = max(limits.photo_bytes, limits.video_bytes)
        if declared is not None and declared > largest:
            raise PayloadTooLargeError(largest)

        # Accept multipart upload (preferred — frontend uses FormData)
        # or raw bytes with a Content-Type header (CLI/scripts). A photo
        # is read under ``limits.photo_bytes``; a video is streamed to
        # disk under ``limits.video_bytes``, never held in memory. Over
        # either raises ``PayloadTooLargeError`` (413) via ``_iter``.
        content_type = self.request.headers.get("Content-Type", "")
        if content_type.startswith("multipart/"):
            try:
                reader = await self.request.multipart()
                field = await reader.next()
            except Exception:
                log.debug(
                    "AlbumItemCollectionView.post: fail-soft error ignored",
                    exc_info=True,
                )
                return web.json_response({"error": "bad_multipart"}, status=400)
            if field is None:
                return web.json_response({"error": "missing file"}, status=422)
            if not isinstance(field, BodyPartReader):
                return web.json_response({"error": "expected file part"}, status=400)
            content_type = field.headers.get("Content-Type", "image/jpeg")
            if is_video_upload(content_type):
                stash = await gallery.stash_upload(
                    iter_part_chunks(field), max_bytes=limits.video_bytes
                )
                upload = await gallery.upload_video(
                    album_id,
                    stash=stash,
                    caption=caption,
                    uploader_user_id=ctx.user_id,
                )
                return await self._uploaded(upload)
            # A photo: its part's headers are read, its body not yet — a
            # declared length over the photo cap is refused here.
            if declared is not None and declared > limits.photo_bytes:
                raise PayloadTooLargeError(limits.photo_bytes)
            data = await read_part_capped(field, limits.photo_bytes)
        else:
            if not content_type:
                content_type = "application/octet-stream"
            if is_video_upload(content_type):
                stash = await gallery.stash_upload(
                    self.request.content.iter_chunked(STREAM_CHUNK_BYTES),
                    max_bytes=limits.video_bytes,
                )
                upload = await gallery.upload_video(
                    album_id,
                    stash=stash,
                    caption=caption,
                    uploader_user_id=ctx.user_id,
                )
                return await self._uploaded(upload)
            data = await read_body_capped(self.request, limits.photo_bytes)

        upload = await gallery.upload_item(
            album_id,
            data=data,
            content_type=content_type,
            caption=caption,
            uploader_user_id=ctx.user_id,
        )
        return await self._uploaded(upload)

    async def _uploaded(self, upload: GalleryUpload) -> web.Response:
        item = upload.item
        payload = _item_signed(self.request, item)
        if upload.outcome is not UploadOutcome.CREATED:
            # The same file is already here (or was in the trash and came
            # back): nothing new was stored. 200 with the existing item, so
            # the SPA can count "already here" rather than a new upload.
            payload["duplicate"] = upload.outcome.value
            return web.json_response(payload, status=200)
        # A freshly-uploaded video transcodes in the background — the
        # ``.webm`` + poster don't exist on disk yet. Tell the SPA to
        # render a "processing" placeholder (with a rough "ready in"); the
        # LIST endpoint derives the live status from the transcode repo
        # on refetch.
        if item.item_type == "video":
            payload["media_status"] = "processing"
            fn = media_filename(item.url)
            if fn is not None:
                etas = await self.svc(K.media_transcode_service_key).estimates([fn])
                if fn in etas:
                    payload["media_eta_s"] = etas[fn]
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


#: Default page of the trash list.
TRASH_PAGE: int = 100


class GalleryTrashCollectionView(BaseView):
    """GET /api/gallery/trash — the household trash the caller may see."""

    async def get(self) -> web.Response:
        ctx = self.user
        await self.svc(K.preferences_service_key).require_enabled(_GALLERY_SECTION)
        try:
            limit = int(self.request.query.get("limit", TRASH_PAGE))
        except ValueError:
            limit = TRASH_PAGE
        rows = await self.svc(K.gallery_service_key).list_trash(
            actor_user_id=ctx.user_id,
            limit=limit,
            before_id=self.request.query.get("before_id") or None,
        )
        return web.json_response([_trash_signed(self.request, t) for t in rows])


class GalleryTrashItemView(BaseView):
    """DELETE /api/gallery/trash/{item_id} — delete one item for good."""

    async def delete(self) -> web.Response:
        ctx = self.user
        await self.svc(K.preferences_service_key).require_enabled(_GALLERY_SECTION)
        await self.svc(K.gallery_service_key).purge_trashed_item(
            self.match("item_id"), actor_user_id=ctx.user_id
        )
        return web.Response(status=204)


class GalleryItemRestoreView(BaseView):
    """POST /api/gallery/items/{item_id}/restore — back from the trash."""

    async def post(self) -> web.Response:
        ctx = self.user
        await self.svc(K.preferences_service_key).require_enabled(_GALLERY_SECTION)
        item = await self.svc(K.gallery_service_key).restore_item(
            self.match("item_id"), actor_user_id=ctx.user_id
        )
        return web.json_response(_item_signed(self.request, item))


def _trash_signed(request: web.Request, t: GalleryTrashItem) -> dict:
    """A trash row: the item as it was (signed URLs), when and by whom it
    was deleted, when the purge removes it, and the album it returns to."""
    payload = _item_signed(request, t.item)
    # ``deleted_at`` is naive UTC (SQLite ``datetime('now')`` shape); the
    # wire carries it tz-aware.
    deleted = datetime.fromisoformat(t.deleted_at)
    if deleted.tzinfo is None:
        deleted = deleted.replace(tzinfo=UTC)
    payload.update(
        {
            "deleted_at": deleted.isoformat(),
            "deleted_by": t.deleted_by,
            "purge_after": (deleted + timedelta(days=TRASH_RETENTION_DAYS)).isoformat(),
            "album_name": t.album_name,
            "album_in_trash": t.album_in_trash,
        }
    )
    return payload


def _on_this_day_settings(s: OnThisDaySettings, today: str) -> dict:
    return {
        "enabled": s.enabled,
        "hidden_today": s.hidden_on == today,
        "excluded_album_ids": sorted(s.excluded_album_ids),
    }


def _on_this_day_card(request: web.Request, c: OnThisDayCard) -> dict:
    return {
        "album": _album_signed(request, c.album),
        "date": c.date,
        "years_ago": c.years_ago,
        "count": c.count,
        "items": [_item_signed(request, i) for i in c.items],
    }


def _flag(body: dict, key: str) -> bool | None:
    """An optional boolean of the body — anything else is a 422."""
    value = body.get(key)
    if value is None or isinstance(value, bool):
        return value
    raise ValueError(f"{key} must be true or false")


def _album_id(body: dict, key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError(f"{key} must be an album id")
    return value


class GalleryOnThisDayView(BaseView):
    """GET/PATCH /api/gallery/on-this-day — the household gallery's "On
    this day" card (owner decision D14): photos taken on the viewer's day
    in an earlier year, from one album open to everyone at home. Never a
    notification; each person turns it off, hides it for the day or
    leaves an album out.

    ``?date=YYYY-MM-DD`` (GET) / ``date`` (PATCH) is the viewer's own
    calendar day — within one day of the server's UTC date, else 422;
    absent, the UTC date."""

    async def get(self) -> web.Response:
        ctx = self.user
        await self.svc(K.preferences_service_key).require_enabled(_GALLERY_SECTION)
        today = resolve_today(self.request.query.get("date"))
        svc = self.svc(K.gallery_on_this_day_service_key)
        card = await svc.card(user_id=ctx.user_id, today=today)
        settings = await svc.settings(ctx.user_id)
        return web.json_response(
            {
                "card": _on_this_day_card(self.request, card) if card else None,
                "settings": _on_this_day_settings(settings, today.isoformat()),
            }
        )

    async def patch(self) -> web.Response:
        ctx = self.user
        await self.svc(K.preferences_service_key).require_enabled(_GALLERY_SECTION)
        body = await self.body()
        raw_date = body.get("date")
        if raw_date is not None and not isinstance(raw_date, str):
            raise ValueError("date must be YYYY-MM-DD")
        today = resolve_today(raw_date)
        hide_today = _flag(body, "hide_today")
        settings = await self.svc(K.gallery_on_this_day_service_key).update(
            ctx.user_id,
            enabled=_flag(body, "enabled"),
            hide_on=today if hide_today else None,
            show_again=hide_today is False,
            exclude_album_id=_album_id(body, "exclude_album_id"),
            include_album_id=_album_id(body, "include_album_id"),
            include_all=_flag(body, "include_all") is True,
        )
        return web.json_response(
            {"settings": _on_this_day_settings(settings, today.isoformat())}
        )
