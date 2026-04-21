"""Gallery service — albums of photos and videos (§23.119).

Albums and items live either at the household level
(``space_id is None``) or scoped to a specific space.

Media pipeline matches feed posts:

* photo → ImageProcessor → WebP (EXIF stripped); a 400 px WebP
  thumbnail is generated for the grid view.
* video → VideoProcessor → VP9/Opus WebM with a thumbnail extracted.

EXIF: only the date (``YYYY-MM-DD``) is retained as ``taken_at`` —
no time, no GPS — so the album answers "when was this taken?" without
leaking precise timestamps or location.

Per §25.6.2 S-9 the federation/sync layer must emit only the
``thumbnail_filename`` for ``gallery_items``; the full file is fetched
on demand via ``gallery_item_full``.
"""

from __future__ import annotations

import io
import logging
import pathlib
import uuid
from dataclasses import replace
from datetime import datetime, timezone

from ..config import Config
from ..domain.events import (
    GalleryAlbumCreated,
    GalleryAlbumDeleted,
    GalleryItemDeleted,
    GalleryItemUploaded,
)
from ..domain.gallery import GalleryAlbum, GalleryItem
from ..domain.media_constraints import (
    CAPTION_MAX,
    VIDEO_MAX_DIMENSION,
)
from ..infrastructure.event_bus import EventBus
from ..media.image_processor import ImageProcessor
from ..media.video_processor import VideoProcessor
from ..repositories.gallery_repo import AbstractGalleryRepo
from ..repositories.space_repo import AbstractSpaceRepo

try:
    from PIL import ExifTags, Image

    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

log = logging.getLogger(__name__)


# ─── Limits (§14.927) ─────────────────────────────────────────────────────

NAME_MAX: int = 80
DESCRIPTION_MAX: int = 500
ALBUMS_PER_SPACE: int = 200


# ─── Errors ──────────────────────────────────────────────────────────────


class GalleryError(Exception):
    """Base error class for gallery operations."""


class GalleryNotFoundError(GalleryError):
    """Album or item not found."""


class GalleryPermissionError(GalleryError):
    """Caller is not allowed to perform this action."""


# ─── Service ─────────────────────────────────────────────────────────────


class GalleryService:
    """CRUD + upload pipeline for gallery albums and items."""

    __slots__ = ("_repo", "_space_repo", "_bus", "_config", "_media_dir")

    def __init__(
        self,
        repo: AbstractGalleryRepo,
        space_repo: AbstractSpaceRepo,
        bus: EventBus,
        config: Config,
    ) -> None:
        self._repo = repo
        self._space_repo = space_repo
        self._bus = bus
        self._config = config
        self._media_dir = pathlib.Path(config.media_path)

    # ─── Albums ───────────────────────────────────────────────────────────

    async def list_albums(
        self,
        *,
        space_id: str | None,
        actor_user_id: str,
        limit: int = 30,
        before: str | None = None,
    ) -> list[GalleryAlbum]:
        """List albums in a space (or household) the actor can see.

        Cover URL is enriched: explicit ``cover_item_id`` if set,
        else the first item's thumbnail.
        """
        if space_id is not None:
            await self._require_member(space_id, actor_user_id)
        albums = await self._repo.list_albums(space_id, limit=limit, before=before)
        out: list[GalleryAlbum] = []
        for a in albums:
            cover_url = await self._resolve_cover(a)
            out.append(_with_cover(a, cover_url))
        return out

    async def get_album(
        self,
        album_id: str,
        *,
        actor_user_id: str,
    ) -> GalleryAlbum:
        album = await self._repo.get_album(album_id)
        if album is None:
            raise GalleryNotFoundError(f"album {album_id!r} not found")
        if album.space_id is not None:
            await self._require_member(album.space_id, actor_user_id)
        cover_url = await self._resolve_cover(album)
        return _with_cover(album, cover_url)

    async def create_album(
        self,
        *,
        space_id: str | None,
        owner_user_id: str,
        name: str,
        description: str | None = None,
    ) -> GalleryAlbum:
        if not name or not name.strip():
            raise ValueError(f"Album name must be 1–{NAME_MAX} characters")
        if len(name) > NAME_MAX:
            raise ValueError(f"Album name must be 1–{NAME_MAX} characters")
        if description and len(description) > DESCRIPTION_MAX:
            raise ValueError(
                f"Description must be {DESCRIPTION_MAX} characters or fewer"
            )
        if space_id is not None:
            await self._require_member(space_id, owner_user_id)
            existing = await self._repo.list_albums(
                space_id, limit=ALBUMS_PER_SPACE + 1
            )
            if len(existing) >= ALBUMS_PER_SPACE:
                raise ValueError(
                    f"Space has reached the {ALBUMS_PER_SPACE}-album limit"
                )

        now = datetime.now(timezone.utc).isoformat()
        album = GalleryAlbum(
            id=uuid.uuid4().hex,
            space_id=space_id,
            owner_user_id=owner_user_id,
            name=name.strip(),
            description=description,
            cover_item_id=None,
            item_count=0,
            cover_url=None,
            created_at=now,
            updated_at=now,
        )
        await self._repo.create_album(album)
        await self._bus.publish(
            GalleryAlbumCreated(
                album_id=album.id,
                space_id=space_id,
                owner_id=owner_user_id,
            )
        )
        return album

    async def update_album(
        self,
        album_id: str,
        *,
        actor_user_id: str,
        name: str | None = None,
        description: str | None = None,
        cover_item_id: str | None = None,
    ) -> None:
        album = await self._repo.get_album(album_id)
        if album is None:
            raise GalleryNotFoundError(f"album {album_id!r} not found")
        await self._require_album_owner_or_admin(album, actor_user_id)

        patch: dict = {}
        if name is not None:
            n = name.strip()
            if not n or len(n) > NAME_MAX:
                raise ValueError(f"Name must be 1–{NAME_MAX} characters")
            patch["name"] = n
        if description is not None:
            if len(description) > DESCRIPTION_MAX:
                raise ValueError(
                    f"Description must be {DESCRIPTION_MAX} chars or fewer"
                )
            patch["description"] = description
        if cover_item_id is not None:
            item = await self._repo.get_item(cover_item_id)
            if item is None or item.album_id != album_id:
                raise ValueError("Cover item must belong to this album")
            patch["cover_item_id"] = cover_item_id
        if patch:
            await self._repo.update_album(album_id, patch)

    async def delete_album(self, album_id: str, *, actor_user_id: str) -> None:
        album = await self._repo.get_album(album_id)
        if album is None:
            return
        await self._require_album_owner_or_admin(album, actor_user_id)
        await self._repo.delete_album(album_id)
        await self._bus.publish(
            GalleryAlbumDeleted(
                album_id=album_id,
                space_id=album.space_id,
            )
        )

    async def set_retention_exempt(
        self,
        album_id: str,
        exempt: bool,
        *,
        actor_user_id: str,
    ) -> None:
        """Mark an album as exempt from space retention purge (§23.132)."""
        album = await self._repo.get_album(album_id)
        if album is None:
            raise GalleryNotFoundError(f"album {album_id!r} not found")
        await self._require_album_owner_or_admin(album, actor_user_id)
        await self._repo.set_retention_exempt(
            album_id,
            exempt,
            space_id=album.space_id,
        )

    # ─── Items ────────────────────────────────────────────────────────────

    async def list_items(
        self,
        album_id: str,
        *,
        actor_user_id: str,
        limit: int = 50,
        before: str | None = None,
    ) -> list[GalleryItem]:
        album = await self._repo.get_album(album_id)
        if album is None:
            raise GalleryNotFoundError(f"album {album_id!r} not found")
        if album.space_id is not None:
            await self._require_member(album.space_id, actor_user_id)
        return await self._repo.list_items(album_id, limit=limit, before=before)

    async def upload_item(
        self,
        album_id: str,
        *,
        data: bytes,
        content_type: str,
        caption: str | None,
        uploader_user_id: str,
    ) -> GalleryItem:
        """Process and store one photo or video.

        Photo path runs the ImageProcessor → WebP, extracts the EXIF
        date (day-only, no GPS), generates a thumbnail.

        Video path runs the VideoProcessor → WebM and uses the
        extracted thumbnail.
        """
        album = await self._repo.get_album(album_id)
        if album is None:
            raise GalleryNotFoundError(f"album {album_id!r} not found")
        if album.space_id is not None:
            await self._require_member(album.space_id, uploader_user_id)
        if caption and len(caption) > CAPTION_MAX:
            raise ValueError(f"Caption must be {CAPTION_MAX} characters or fewer")
        if not data:
            raise ValueError("upload data is empty")

        is_video = (
            content_type.startswith("video/")
            or content_type == "application/octet-stream"
        )

        if is_video:
            item = await self._upload_video(
                album_id=album_id,
                data=data,
                content_type=content_type or "video/mp4",
                caption=caption,
                uploader_user_id=uploader_user_id,
            )
        else:
            item = await self._upload_photo(
                album_id=album_id,
                data=data,
                content_type=content_type,
                caption=caption,
                uploader_user_id=uploader_user_id,
            )

        await self._repo.create_item(item)
        await self._repo.increment_item_count(album_id, +1)
        await self._bus.publish(
            GalleryItemUploaded(
                item_id=item.id,
                album_id=album_id,
                item_type=item.item_type,
                uploader=uploader_user_id,
            )
        )
        return item

    async def delete_item(
        self,
        item_id: str,
        *,
        actor_user_id: str,
    ) -> None:
        item = await self._repo.get_item(item_id)
        if item is None:
            return
        album = await self._repo.get_album(item.album_id)
        if album is not None and album.space_id is not None:
            is_uploader = item.uploaded_by == actor_user_id
            is_admin = await self._is_space_admin(album.space_id, actor_user_id)
            if not (is_uploader or is_admin):
                raise GalleryPermissionError(
                    "Only the uploader or a space admin may delete this item"
                )
        await self._repo.delete_item(item_id)
        await self._repo.increment_item_count(item.album_id, -1)
        await self._bus.publish(
            GalleryItemDeleted(
                item_id=item_id,
                album_id=item.album_id,
            )
        )

    # ─── Internals: media pipeline ────────────────────────────────────────

    async def _upload_photo(
        self,
        *,
        album_id: str,
        data: bytes,
        content_type: str,
        caption: str | None,
        uploader_user_id: str,
    ) -> GalleryItem:
        proc = ImageProcessor()
        # Extract EXIF date BEFORE processing — ImageProcessor strips EXIF.
        taken_at = self._extract_exif_date(data)

        out_bytes, out_name = await proc.process(data, "upload")
        self._save_to_disk(out_name, out_bytes)

        # Delegate to ImageProcessor.generate_thumbnail — one path for all
        # WebP thumbnails (EXIF-aware, LANCZOS, THUMBNAIL_WEBP_QUALITY).
        try:
            thumb_bytes = await proc.generate_thumbnail(out_bytes)
            thumb_name = f"{uuid.uuid4().hex}.webp"
            self._save_to_disk(thumb_name, thumb_bytes)
            thumbnail_url = f"/api/media/{thumb_name}"
        except ValueError as exc:
            log.warning("gallery: photo thumbnail failed, using primary: %s", exc)
            thumbnail_url = f"/api/media/{out_name}"

        w, h = self._read_dims(self._media_dir / out_name)
        return GalleryItem(
            id=uuid.uuid4().hex,
            album_id=album_id,
            uploaded_by=uploader_user_id,
            item_type="photo",
            url=f"/api/media/{out_name}",
            thumbnail_url=thumbnail_url,
            width=w,
            height=h,
            duration_s=None,
            caption=caption,
            taken_at=taken_at,
            sort_order=0,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    async def _upload_video(
        self,
        *,
        album_id: str,
        data: bytes,
        content_type: str,
        caption: str | None,
        uploader_user_id: str,
    ) -> GalleryItem:
        proc = VideoProcessor()
        out_bytes, out_name = await proc.process(data, "upload.mp4")
        self._save_to_disk(out_name, out_bytes)

        # Extract a WebP thumbnail from the first video frame.
        thumb_name = f"{uuid.uuid4().hex}.webp"
        try:
            thumb_bytes = await proc.generate_thumbnail(data)
            self._save_to_disk(thumb_name, thumb_bytes)
        except (RuntimeError, ValueError) as exc:
            log.warning("gallery: video thumbnail extraction failed: %s", exc)

        return GalleryItem(
            id=uuid.uuid4().hex,
            album_id=album_id,
            uploaded_by=uploader_user_id,
            item_type="video",
            url=f"/api/media/{out_name}",
            thumbnail_url=f"/api/media/{thumb_name}",
            width=VIDEO_MAX_DIMENSION,
            height=int(VIDEO_MAX_DIMENSION * 9 / 16),
            duration_s=None,
            caption=caption,
            taken_at=None,
            sort_order=0,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def _save_to_disk(self, filename: str, payload: bytes) -> None:
        self._media_dir.mkdir(parents=True, exist_ok=True)
        (self._media_dir / filename).write_bytes(payload)

    @staticmethod
    def _extract_exif_date(data: bytes) -> str | None:
        """Pull DateTimeOriginal as ``YYYY-MM-DD`` (day precision only)."""
        if not _PIL_AVAILABLE:
            return None
        try:
            img = Image.open(io.BytesIO(data))
            exif = img.getexif()
            if not exif:
                return None
            for tag_id, val in exif.items():
                if ExifTags.TAGS.get(tag_id) == "DateTimeOriginal":
                    parts = str(val).split(" ", 1)[0].replace(":", "-")
                    return parts
        except Exception:
            return None
        return None

    @staticmethod
    def _read_dims(path: pathlib.Path) -> tuple[int, int]:
        try:
            if not _PIL_AVAILABLE:  # pragma: no cover
                return 0, 0
            with Image.open(path) as img:
                return int(img.width), int(img.height)
        except Exception:  # pragma: no cover
            return 0, 0

    # ─── Internals: cover / permissions ───────────────────────────────────

    async def _resolve_cover(self, album: GalleryAlbum) -> str | None:
        if album.cover_item_id:
            item = await self._repo.get_item(album.cover_item_id)
            if item is not None:
                return item.thumbnail_url
        return await self._repo.get_first_item_thumbnail(album.id)

    async def _require_member(self, space_id: str, user_id: str) -> None:
        member = await self._space_repo.get_member(space_id, user_id)
        if member is None:
            raise GalleryPermissionError(
                f"user {user_id!r} is not a member of space {space_id!r}"
            )

    async def _is_space_admin(self, space_id: str, user_id: str) -> bool:
        member = await self._space_repo.get_member(space_id, user_id)
        return member is not None and member.role in ("owner", "admin")

    async def _require_album_owner_or_admin(
        self,
        album: GalleryAlbum,
        actor_user_id: str,
    ) -> None:
        if album.owner_user_id == actor_user_id:
            return
        if album.space_id is not None and await self._is_space_admin(
            album.space_id,
            actor_user_id,
        ):
            return
        raise GalleryPermissionError(
            "Only the album owner or a space admin can perform this action"
        )


# ─── Helpers ─────────────────────────────────────────────────────────────


def _with_cover(album: GalleryAlbum, cover_url: str | None) -> GalleryAlbum:
    """Return a copy of *album* with ``cover_url`` filled in."""
    return replace(album, cover_url=cover_url)
