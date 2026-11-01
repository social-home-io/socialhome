"""Gallery service — albums of photos and videos (§23.119).

Albums and items live either at the household level
(``space_id is None``) or scoped to a specific space.

Media pipeline matches feed posts:

* photo → ImageProcessor → WebP (EXIF stripped); a 400 px WebP
  thumbnail is generated for the grid view.
* video → VideoProcessor → VP9/Opus WebM with a thumbnail extracted.

EXIF: only the date (``YYYY-MM-DD``) is retained as ``taken_at`` —
no time, no GPS — so the album answers "when was this taken?" without
leaking precise timestamps or location. It is read from the Exif
sub-IFD (where cameras write it) before the processor strips all EXIF
from the stored file.

§25.6.2 S-9 describes a thumbnail-only federation projection with an
on-demand ``gallery_item_full`` fetch; that fetch was never built, so the
federated item carries its full ``url`` and both files are pushed over
the media outbox (see ``GalleryItem.to_federation_dict``).
"""

from __future__ import annotations

import asyncio
import io
import logging
import pathlib
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import aiofiles
import aiofiles.os

from ..config import Config
from ..domain.events import (
    GalleryAlbumCreated,
    GalleryAlbumDeleted,
    GalleryAlbumUpdated,
    GalleryItemDeleted,
    GalleryItemUploaded,
)
from ..domain.gallery import GalleryAlbum, GalleryItem
from ..domain.media_constraints import (
    CAPTION_MAX,
    VIDEO_MAX_DIMENSION,
)
from ..domain.post import Post, PostType
from ..domain.preferences import FeatureDisabledError
from ..domain.space import (
    CONTENT_AUTHORITY_ROLES,
    SETTINGS_AUTHORITY_ROLES,
    WRITER_ROLES,
    SpaceRole,
    SubscriberReadOnlyError,
)
from ..federation.owner_bound_id import (
    GALLERY_ALBUM_KIND,
    GALLERY_ITEM_KIND,
    mint_owner_bound_id,
)
from ..infrastructure.event_bus import EventBus
from ..media.cleanup import unlink_unreferenced
from ..media.image_processor import ImageProcessor
from ..repositories.gallery_repo import AbstractGalleryRepo
from ..repositories.media_reference_repo import AbstractMediaReferenceRepo
from ..repositories.media_transcode_repo import AbstractMediaTranscodeRepo
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.user_repo import AbstractUserRepo
from .media_transcode_service import MediaTranscodeService
from .space_member_guard import NOT_A_SPACE_MEMBER, seat_of
from .storage_quota_service import StorageQuotaService

try:
    from PIL import ExifTags, Image

    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

log = logging.getLogger(__name__)


#: EXIF date-time layout: ``YYYY:MM:DD HH:MM:SS`` (EXIF 2.3 §4.6.5).
_EXIF_DATETIME_FORMAT = "%Y:%m:%d %H:%M:%S"


def _parse_exif_day(value: object) -> str | None:
    """``YYYY:MM:DD HH:MM:SS`` → ``YYYY-MM-DD``, or ``None`` if malformed.

    Cameras write blanks or ``0000:00:00 00:00:00`` when the clock was
    never set; those (and any impossible date) are no date at all.
    """
    if not isinstance(value, str):
        return None
    try:
        when = datetime.strptime(value.strip("\x00 "), _EXIF_DATETIME_FORMAT)
    except ValueError:
        return None
    return when.date().isoformat()


def _extract_exif_date_sync(data: bytes) -> str | None:
    """The day a photo was taken, from its EXIF, as ``YYYY-MM-DD``.

    Cameras and phones (JPEG and HEIC alike) write DateTimeOriginal /
    DateTimeDigitized into the Exif sub-IFD (0x8769), not IFD0 — so the
    sub-IFD is read first, then IFD0's DateTime (the file's change date)
    as the last resort. Only date tags are read: never GPS.

    The day is the camera's own calendar day. EXIF date-times are local
    wall time; OffsetTimeOriginal is deliberately not applied, as moving
    a late-evening shot into UTC would file it under the next day. No
    time is kept, so there is no UTC timestamp to store.
    """
    if not _PIL_AVAILABLE:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            exif = img.getexif()
            sub = exif.get_ifd(ExifTags.IFD.Exif)
            candidates = (
                sub.get(ExifTags.Base.DateTimeOriginal),
                sub.get(ExifTags.Base.DateTimeDigitized),
                # Pre-fix lookup: a few writers put DateTimeOriginal in IFD0.
                exif.get(ExifTags.Base.DateTimeOriginal),
                exif.get(ExifTags.Base.DateTime),
            )
    except Exception:
        log.debug("_extract_exif_date_sync: fail-soft error ignored", exc_info=True)
        return None
    for value in candidates:
        day = _parse_exif_day(value)
        if day is not None:
            return day
    return None


def _read_dims_sync(path: pathlib.Path) -> tuple[int, int]:
    try:
        if not _PIL_AVAILABLE:  # pragma: no cover
            return 0, 0
        with Image.open(path) as img:
            return int(img.width), int(img.height)
    except Exception:  # pragma: no cover
        log.debug("_read_dims_sync: fail-soft error ignored", exc_info=True)
        return 0, 0


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

    __slots__ = (
        "_bus",
        "_config",
        "_media_dir",
        "_media_refs",
        "_repo",
        "_space_repo",
        "_storage_quota",
        "_transcode_repo",
        "_transcode_service",
        "_users",
    )

    def __init__(
        self,
        repo: AbstractGalleryRepo,
        space_repo: AbstractSpaceRepo,
        bus: EventBus,
        config: Config,
        *,
        media_transcode_repo: AbstractMediaTranscodeRepo | None = None,
        media_transcode_service: MediaTranscodeService | None = None,
        media_refs: AbstractMediaReferenceRepo | None = None,
        user_repo: AbstractUserRepo | None = None,
        storage_quota: StorageQuotaService | None = None,
    ) -> None:
        self._repo = repo
        self._space_repo = space_repo
        self._bus = bus
        self._config = config
        self._media_dir = pathlib.Path(config.media_path)
        # A deleted item's files go only once no other row references them.
        self._media_refs = media_refs
        # Background video transcode (§async-video). When wired, the
        # video upload path stashes source bytes + enqueues a job and
        # returns a "processing" item immediately; the scheduler
        # transcodes off the request path. Optional so unit tests that
        # only exercise photos / album CRUD can omit them.
        self._transcode_repo = media_transcode_repo
        self._transcode_service = media_transcode_service
        # Household admins manage every household album and item. Without
        # the user repo nobody is an admin here — the safe default.
        self._users = user_repo
        # The household's byte budget (§5.2) — the same guard rail
        # ``POST /api/media/upload`` applies, so the gallery (the biggest
        # disk consumer) can't fill the disk past it.
        self._storage_quota = storage_quota

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
        album = await self._visible_album(album_id, actor_user_id)
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
            await self._require_writer(space_id, owner_user_id)
            existing = await self._repo.list_albums(
                space_id, limit=ALBUMS_PER_SPACE + 1
            )
            if len(existing) >= ALBUMS_PER_SPACE:
                raise ValueError(
                    f"Space has reached the {ALBUMS_PER_SPACE}-album limit"
                )

        now = datetime.now(UTC).isoformat()
        album = GalleryAlbum(
            # A space album federates, so its id commits to its creator —
            # no other household can announce it first as theirs. A
            # household album never leaves this household.
            id=(
                uuid.uuid4().hex
                if space_id is None
                else mint_owner_bound_id(
                    GALLERY_ALBUM_KIND,
                    space_id=space_id,
                    owner_user_id=owner_user_id,
                )
            ),
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
        album = await self._visible_album(album_id, actor_user_id)
        if album.is_system:
            raise GalleryPermissionError(
                "system album cannot be renamed or edited",
            )
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
            await self._bus.publish(
                GalleryAlbumUpdated(album_id=album_id, space_id=album.space_id)
            )

    async def delete_album(self, album_id: str, *, actor_user_id: str) -> None:
        album = await self._repo.get_album(album_id)
        if album is None or not await self._can_see(album, actor_user_id):
            # Idempotent, and an album of a space the caller holds no seat
            # in answers exactly like an unknown id.
            return
        await self._require_gallery_on(album)
        if album.is_system:
            raise GalleryPermissionError(
                "system album cannot be deleted",
            )
        await self._require_album_owner_or_admin(album, actor_user_id)
        if await self._is_protected(
            actor_user_id
        ) and await self._repo.has_items_by_others(album_id, actor_user_id):
            # Deleting the album would delete other people's photos with it.
            raise GalleryPermissionError(
                "A protected account may only delete its own uploads"
            )
        media = await self._repo.list_album_media(album_id)
        # A space album keeps a tombstone (§25.6 ``gallery_albums_deleted``);
        # a household album is removed.
        await self._repo.delete_album(album_id, deleted_by=actor_user_id)
        # The item rows are gone (household) or file-free tombstones (space):
        # drop their files unless another row still names them (same rule as
        # ``delete_item``).
        await unlink_unreferenced(self._media_dir, self._media_refs, media)
        await self._bus.publish(
            GalleryAlbumDeleted(
                album_id=album_id,
                space_id=album.space_id,
                owner_id=album.owner_user_id,
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
        album = await self._visible_album(album_id, actor_user_id)
        if album.is_system:
            raise GalleryPermissionError(
                "system album retention is managed automatically",
            )
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
        await self._visible_album(album_id, actor_user_id)
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
        album = await self._visible_album(album_id, uploader_user_id)
        if album.is_system:
            raise GalleryPermissionError(
                "system album cannot be uploaded to directly — "
                "share a photo or video via a feed post instead",
            )
        if album.space_id is not None:
            await self._require_writer(album.space_id, uploader_user_id)
        if caption and len(caption) > CAPTION_MAX:
            raise ValueError(f"Caption must be {CAPTION_MAX} characters or fewer")
        if not data:
            raise ValueError("upload data is empty")
        if self._storage_quota is not None:
            # 507 STORAGE_FULL via ``BaseView._iter``, before any disk write.
            await self._storage_quota.check_can_store(len(data))

        is_video = (
            content_type.startswith("video/")
            or content_type == "application/octet-stream"
        )

        if is_video:
            item = await self._upload_video(
                album_id=album_id,
                space_id=album.space_id,
                data=data,
                content_type=content_type or "video/mp4",
                caption=caption,
                uploader_user_id=uploader_user_id,
            )
        else:
            item = await self._upload_photo(
                album_id=album_id,
                space_id=album.space_id,
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
                space_id=album.space_id,
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
        if album is not None and not await self._can_see(album, actor_user_id):
            return  # an item of a space the caller isn't in: like an unknown id
        if album is not None:
            await self._require_gallery_on(album)
        if album is not None and album.is_system:
            raise GalleryPermissionError(
                "system album items are managed automatically — "
                "delete the source post to remove this item",
            )
        if album is not None and item.uploaded_by != actor_user_id:
            if await self._is_protected(actor_user_id):
                # A protected account (a child) removes only what it added
                # — whatever album or space role it holds.
                raise GalleryPermissionError(
                    "A protected account may only delete its own uploads"
                )
            if album.space_id is not None:
                if not await self._has_content_authority(album.space_id, actor_user_id):
                    raise GalleryPermissionError(
                        "Only the uploader or a space moderator may delete this item"
                    )
            elif not (
                album.owner_user_id == actor_user_id
                or await self._is_household_admin(actor_user_id)
            ):
                # A household photo: its uploader, the album's owner or a
                # household admin — not every member of the household.
                raise GalleryPermissionError(
                    "Only the uploader, the album owner or a household admin "
                    "may delete this item"
                )
        # A space item keeps a file-free tombstone (§25.6
        # ``gallery_items_deleted``); a household item is removed.
        await self._repo.delete_item(item_id, deleted_by=actor_user_id)
        await self._repo.increment_item_count(item.album_id, -1)
        # Drop the backing file(s) unless another row still references
        # them (an item synced from another household may name a file
        # some other row owns). Best effort; a missing file is fine.
        await unlink_unreferenced(
            self._media_dir, self._media_refs, [item.url, item.thumbnail_url]
        )
        await self._bus.publish(
            GalleryItemDeleted(
                item_id=item_id,
                album_id=item.album_id,
                space_id=album.space_id if album is not None else None,
            )
        )

    # ─── Internals: media pipeline ────────────────────────────────────────

    async def _upload_photo(
        self,
        *,
        album_id: str,
        space_id: str | None,
        data: bytes,
        content_type: str,
        caption: str | None,
        uploader_user_id: str,
    ) -> GalleryItem:
        proc = ImageProcessor()
        # Extract EXIF date BEFORE processing — ImageProcessor strips EXIF.
        taken_at = await self._extract_exif_date(data)

        out_bytes, out_name = await proc.process(data, "upload")
        await self._save_to_disk(out_name, out_bytes)

        # Delegate to ImageProcessor.generate_thumbnail — one path for all
        # WebP thumbnails (EXIF-aware, LANCZOS, THUMBNAIL_WEBP_QUALITY).
        try:
            thumb_bytes = await proc.generate_thumbnail(out_bytes)
            thumb_name = f"{uuid.uuid4().hex}.webp"
            await self._save_to_disk(thumb_name, thumb_bytes)
            thumbnail_url = f"api/media/{thumb_name}"
        except ValueError as exc:
            log.warning("gallery: photo thumbnail failed, using primary: %s", exc)
            thumbnail_url = f"api/media/{out_name}"

        w, h = await self._read_dims(self._media_dir / out_name)
        return GalleryItem(
            id=_item_id(space_id, uploader_user_id),
            album_id=album_id,
            uploaded_by=uploader_user_id,
            item_type="photo",
            url=f"api/media/{out_name}",
            thumbnail_url=thumbnail_url,
            width=w,
            height=h,
            duration_s=None,
            caption=caption,
            taken_at=taken_at,
            sort_order=0,
            created_at=datetime.now(UTC).isoformat(),
        )

    async def _upload_video(
        self,
        *,
        album_id: str,
        space_id: str | None,
        data: bytes,
        content_type: str,
        caption: str | None,
        uploader_user_id: str,
    ) -> GalleryItem:
        """Async video upload — enqueue a transcode job, return immediately.

        Mints the eventual ``.webm`` output + ``.webp`` thumbnail names,
        stashes the raw source bytes under the (non-served)
        ``transcode_src`` temp dir, enqueues a ``media_transcode_jobs``
        row keyed by the output filename, and nudges the scheduler. The
        :class:`GalleryItem` is created with placeholder dims and
        ``duration_s=None`` — the SPA renders a "processing" placeholder
        until :meth:`MediaTranscodeService.flush_once` writes the files
        and clears the row.

        Requires the transcode repo + service to be wired; without them
        there is no background worker to drain the queue, so we fail loud
        rather than silently dropping the upload.
        """
        if self._transcode_repo is None or self._transcode_service is None:
            raise RuntimeError(
                "video upload requires the media transcode service to be wired"
            )
        # ONE shared UUID stem for the transcoded ``.webm`` and its
        # ``.webp`` poster so the poster path is derivable from the
        # media URL server-side (matches the feed/DM upload path).
        stem = uuid.uuid4().hex
        output_filename = f"{stem}.webm"
        thumbnail_filename = f"{stem}.webp"
        source_path = await self._stash_transcode_source(data)
        await self._transcode_repo.enqueue(
            output_filename=output_filename,
            source_path=str(source_path),
            thumbnail_filename=thumbnail_filename,
            owner_user_id=uploader_user_id,
        )
        self._transcode_service.nudge()

        return GalleryItem(
            id=_item_id(space_id, uploader_user_id),
            album_id=album_id,
            uploaded_by=uploader_user_id,
            item_type="video",
            url=f"api/media/{output_filename}",
            thumbnail_url=f"api/media/{thumbnail_filename}",
            width=VIDEO_MAX_DIMENSION,
            height=int(VIDEO_MAX_DIMENSION * 9 / 16),
            duration_s=None,
            caption=caption,
            taken_at=None,
            sort_order=0,
            created_at=datetime.now(UTC).isoformat(),
        )

    async def _stash_transcode_source(self, data: bytes) -> pathlib.Path:
        """Write raw upload bytes to the (non-served) transcode temp dir.

        ``transcode_src`` is a subdir of the media root; the media serve
        route rejects any filename containing ``/`` so these temp blobs
        can never be fetched via ``/api/media/{filename}``. The transcode
        scheduler reads + deletes them; the same dir is the one
        :class:`MediaTranscodeService` is given as ``media_dir``'s root.
        """
        temp_dir = self._media_dir / "transcode_src"
        await aiofiles.os.makedirs(temp_dir, exist_ok=True)
        temp_path = temp_dir / f"{uuid.uuid4().hex}.bin"
        async with aiofiles.open(temp_path, "wb") as f:
            await f.write(data)
        return temp_path

    async def _save_to_disk(self, filename: str, payload: bytes) -> None:
        await aiofiles.os.makedirs(self._media_dir, exist_ok=True)
        async with aiofiles.open(self._media_dir / filename, "wb") as f:
            await f.write(payload)

    async def _extract_exif_date(self, data: bytes) -> str | None:
        """The EXIF "taken at" day as ``YYYY-MM-DD`` (day precision only)."""
        return await asyncio.to_thread(_extract_exif_date_sync, data)

    async def _read_dims(self, path: pathlib.Path) -> tuple[int, int]:
        return await asyncio.to_thread(_read_dims_sync, path)

    # ─── Internals: cover / permissions ───────────────────────────────────

    async def _resolve_cover(self, album: GalleryAlbum) -> str | None:
        if album.cover_item_id:
            item = await self._repo.get_item(album.cover_item_id)
            # Only an item of this album: a cover id can name one that has
            # not arrived yet, or (from the wire) one filed elsewhere.
            if item is not None and item.album_id == album.id:
                return item.thumbnail_url
        return await self._repo.get_first_item_thumbnail(album.id)

    async def _require_member(self, space_id: str, user_id: str) -> None:
        """A seat in ``space_id`` (any role, a follower included — a
        banned user's leftover seat counts as none), else 403 ``Not a space
        member.`` — the space gate's own answer, naming nobody."""
        if await seat_of(self._space_repo, space_id, user_id) is None:
            raise GalleryPermissionError(NOT_A_SPACE_MEMBER)

    async def _require_writer(self, space_id: str, user_id: str) -> None:
        """A member's seat: adding to a space gallery is a write, so a
        read-only follower gets 403 ``SUBSCRIBER_READ_ONLY``."""
        seat = await seat_of(self._space_repo, space_id, user_id)
        if seat is None:
            raise GalleryPermissionError(NOT_A_SPACE_MEMBER)
        if seat.role not in WRITER_ROLES:
            raise SubscriberReadOnlyError("upload")

    async def _can_see(self, album: GalleryAlbum, user_id: str) -> bool:
        """A household album: every local user. A space album: a seat in
        its space (:func:`seat_of` — not a banned user's leftover)."""
        if album.space_id is None:
            return True
        return await seat_of(self._space_repo, album.space_id, user_id) is not None

    async def _visible_album(self, album_id: str, user_id: str) -> GalleryAlbum:
        """The album, for someone who may see it — else the unknown id's
        404, so the by-id routes (outside the ``/api/spaces/{id}`` seat
        gate) never tell an outsider that an album, or its space, exists."""
        album = await self._repo.get_album(album_id)
        if album is None or not await self._can_see(album, user_id):
            raise GalleryNotFoundError(f"album {album_id!r} not found")
        await self._require_gallery_on(album)
        return album

    async def _require_gallery_on(self, album: GalleryAlbum) -> None:
        """403 ``FEATURE_DISABLED`` (``space:gallery``) for an album of a
        space whose admin switched the gallery off — the by-id routes
        answer like the space-scoped ones. Asked only after the seat
        check, so an outsider still gets the unknown id's 404."""
        if album.space_id is None:
            return
        space = await self._space_repo.get(album.space_id)
        if space is not None and not space.features.gallery:
            raise FeatureDisabledError("space:gallery")

    async def _has_role(
        self, space_id: str, user_id: str, roles: frozenset[SpaceRole]
    ) -> bool:
        member = await self._space_repo.get_member(space_id, user_id)
        return member is not None and member.role in roles

    async def _has_content_authority(self, space_id: str, user_id: str) -> bool:
        """Owner / admin / moderator — may act on others' gallery content."""
        return await self._has_role(space_id, user_id, CONTENT_AUTHORITY_ROLES)

    async def _require_album_owner_or_admin(
        self,
        album: GalleryAlbum,
        actor_user_id: str,
    ) -> None:
        """The album's owner, or — for a space album — settings authority
        (owner / admin), for a household album a household admin.

        A whole album — rename, delete, retention — is not moderation: a
        moderator acts on single items (:meth:`delete_item`). A household
        admin has no say over a space album: the space's own roles do."""
        if album.owner_user_id == actor_user_id:
            return
        if album.space_id is None:
            if await self._is_household_admin(actor_user_id):
                return
            raise GalleryPermissionError(
                "Only the album owner or a household admin can perform this action"
            )
        if await self._has_role(
            album.space_id, actor_user_id, SETTINGS_AUTHORITY_ROLES
        ):
            return
        raise GalleryPermissionError(
            "Only the album owner or a space admin can perform this action"
        )

    async def _is_household_admin(self, user_id: str) -> bool:
        """A local admin of this household (``users.is_admin``)."""
        if self._users is None:
            return False
        user = await self._users.get_by_user_id(user_id)
        return user is not None and user.is_admin and user.is_active()

    async def _is_protected(self, user_id: str) -> bool:
        """A local account under child protection (§CP) — it deletes only
        its own uploads."""
        if self._users is None:
            return False
        user = await self._users.get_by_user_id(user_id)
        return user is not None and user.child_protection_enabled

    # ─── System album (auto-mirror of feed media) ─────────────────────────
    #
    # The "Posts" album is created lazily on the first post that ships
    # media in a given scope and is kept in sync with the source posts
    # via :class:`SystemAlbumBridge`. The methods below are the
    # bridge's only entry points — direct user-facing routes are gated
    # in :meth:`upload_item` / :meth:`delete_item` etc.

    SYSTEM_ALBUM_NAME: str = "Posts"
    SYSTEM_ALBUM_DESCRIPTION: str = (
        "Photos and videos shared to the feed appear here automatically."
    )

    async def ensure_system_album(
        self,
        space_id: str | None,
    ) -> GalleryAlbum:
        """Idempotent get-or-create. Race-safe via the partial unique index."""
        existing = await self._repo.get_system_album(space_id)
        if existing is not None:
            return existing
        now = datetime.now(UTC).isoformat()
        album = GalleryAlbum(
            id=uuid.uuid4().hex,
            space_id=space_id,
            owner_user_id=None,
            name=self.SYSTEM_ALBUM_NAME,
            description=self.SYSTEM_ALBUM_DESCRIPTION,
            cover_item_id=None,
            item_count=0,
            cover_url=None,
            retention_exempt=True,
            is_system=True,
            created_at=now,
            updated_at=now,
        )
        # ON CONFLICT DO NOTHING in the repo means a race-loser's INSERT
        # is silently dropped; we re-SELECT to find the winner's row.
        await self._repo.create_album(album)
        winner = await self._repo.get_system_album(space_id)
        return winner if winner is not None else album

    async def mirror_post(
        self,
        post: Post,
        space_id: str | None,
        *,
        quiet: bool = False,
    ) -> None:
        """Mirror a post's media into the system album for the scope.

        Called by :class:`SystemAlbumBridge` on PostCreated / PostEdited
        / SpacePostCreated / SpacePostSynced. Idempotent: an edit that
        doesn't change the media URLs is a no-op (skips churning rows +
        events), and two mirrors of the same post at once store each file
        once (the unique index of migration 0091; ``create_item`` is
        ``ON CONFLICT DO NOTHING``). ``quiet``: publish no
        ``GalleryItemUploaded`` per item — a §25.6 catch-up is history, and
        a large one would send one live frame per photo.
        """

        # Only image / video posts contribute media. Text, file, and
        # poll posts skip this path entirely. Normalise the URL form for
        # the diff below: the repo always reconstructs items as
        # ``api/media/{filename}`` (no leading slash); a post carrying
        # ``/api/media/{filename}`` from a legacy code path would
        # otherwise look "different" and churn the system album on a
        # plain text edit.
        def _normalise(u: str) -> str:
            return u[1:] if u.startswith("/api/") else u

        urls: list[tuple[str, str]] = []  # [(url, item_type), ...]
        if post.type is PostType.IMAGE:
            urls = [(_normalise(u), "photo") for u in post.image_urls if u]
        elif post.type is PostType.VIDEO and post.media_url:
            urls = [(_normalise(post.media_url), "video")]
        if not urls:
            # Either text-only post or a previously-image post that
            # had its media stripped on edit. Drop any orphans.
            await self.unmirror_post(post.id)
            return

        # Diff against existing rows so a text-only edit on an image
        # post doesn't churn the system album.
        existing = await self._repo.list_items_by_source_post(post.id)
        existing_urls = {(it.url, it.item_type) for it in existing}
        if existing_urls == set(urls):
            return  # no change — skip the delete-then-insert

        album = await self.ensure_system_album(space_id)
        # Delete-then-insert. Both writes are queued on the same
        # AsyncDatabase; the queue serialises them so a partial state
        # is never observable to readers (the SQLite WAL bundles the
        # batch into one transaction).
        if existing_urls:
            await self._repo.delete_items_by_source_post(post.id)
        now = datetime.now(UTC).isoformat()
        for url, item_type in urls:
            item = GalleryItem(
                id=uuid.uuid4().hex,
                album_id=album.id,
                uploaded_by=post.author,
                item_type=item_type,
                url=url,
                # The post's media is its own thumbnail — the upload
                # pipeline already resized images and extracted video
                # frames, and the gallery row stores filename only.
                # Re-using the same filename means no extra disk write.
                thumbnail_url=url,
                width=0,
                height=0,
                source_post_id=post.id,
                created_at=now,
            )
            await self._repo.create_item(item)
            if quiet:
                continue
            await self._bus.publish(
                GalleryItemUploaded(
                    item_id=item.id,
                    album_id=album.id,
                    item_type=item.item_type,
                    uploader=post.author,
                    space_id=album.space_id,
                )
            )
        await self._repo.recount_items(album.id)

    async def unmirror_post(self, post_id: str) -> None:
        """Remove every system-album item mirrored from ``post_id``.

        Called by :class:`SystemAlbumBridge` on PostDeleted /
        SpacePostModerated. The lookup is via the
        ``idx_gallery_items_source_post`` index — O(1) regardless of
        scope, so the bridge doesn't need to know whether the post
        lived in the household feed or a space.
        """
        album_id, count = await self._repo.delete_items_by_source_post(post_id)
        if album_id is None or count == 0:
            return
        await self._repo.increment_item_count(album_id, -count)
        # No per-item GalleryItemDeleted events: the post-delete event
        # already drove this; emitting a flood of item-delete frames
        # here would only duplicate the WS notification.


# ─── Helpers ─────────────────────────────────────────────────────────────


def _with_cover(album: GalleryAlbum, cover_url: str | None) -> GalleryAlbum:
    """Return a copy of *album* with ``cover_url`` filled in."""
    return replace(album, cover_url=cover_url)


def _item_id(space_id: str | None, uploader_user_id: str) -> str:
    """A new upload's id: owner-bound (v_36) when it federates with a space
    album — no other household can announce it first as theirs."""
    if space_id is None:
        return uuid.uuid4().hex
    return mint_owner_bound_id(
        GALLERY_ITEM_KIND, space_id=space_id, owner_user_id=uploader_user_id
    )
