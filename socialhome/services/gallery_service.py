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
from datetime import datetime, timezone

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
from ..domain.space import SpaceRole
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
from .media_transcode_service import MediaTranscodeService

try:
    from PIL import ExifTags, Image

    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

log = logging.getLogger(__name__)


def _extract_exif_date_sync(data: bytes) -> str | None:
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


def _read_dims_sync(path: pathlib.Path) -> tuple[int, int]:
    try:
        if not _PIL_AVAILABLE:  # pragma: no cover
            return 0, 0
        with Image.open(path) as img:
            return int(img.width), int(img.height)
    except Exception:  # pragma: no cover
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
        "_repo",
        "_space_repo",
        "_bus",
        "_config",
        "_media_dir",
        "_media_refs",
        "_transcode_repo",
        "_transcode_service",
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
        album = await self._repo.get_album(album_id)
        if album is None:
            raise GalleryNotFoundError(f"album {album_id!r} not found")
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
        if album is None:
            return
        if album.is_system:
            raise GalleryPermissionError(
                "system album cannot be deleted",
            )
        await self._require_album_owner_or_admin(album, actor_user_id)
        media = await self._repo.list_album_media(album_id)
        await self._repo.delete_album(album_id)
        # The cascade took the item rows; drop their files unless another
        # row still names them (same rule as ``delete_item``).
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
        album = await self._repo.get_album(album_id)
        if album is None:
            raise GalleryNotFoundError(f"album {album_id!r} not found")
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
        if album.is_system:
            raise GalleryPermissionError(
                "system album cannot be uploaded to directly — "
                "share a photo or video via a feed post instead",
            )
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
        if album is not None and album.is_system:
            raise GalleryPermissionError(
                "system album items are managed automatically — "
                "delete the source post to remove this item",
            )
        if album is not None and album.space_id is not None:
            is_uploader = item.uploaded_by == actor_user_id
            is_admin = await self._is_space_admin(album.space_id, actor_user_id)
            if not (is_uploader or is_admin):
                raise GalleryPermissionError(
                    "Only the uploader or a space admin may delete this item"
                )
        await self._repo.delete_item(item_id)
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
            created_at=datetime.now(timezone.utc).isoformat(),
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
            created_at=datetime.now(timezone.utc).isoformat(),
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
        """Pull DateTimeOriginal as ``YYYY-MM-DD`` (day precision only)."""
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
        member = await self._space_repo.get_member(space_id, user_id)
        if member is None:
            raise GalleryPermissionError(
                f"user {user_id!r} is not a member of space {space_id!r}"
            )

    async def _is_space_admin(self, space_id: str, user_id: str) -> bool:
        member = await self._space_repo.get_member(space_id, user_id)
        return member is not None and member.role in (
            SpaceRole.OWNER,
            SpaceRole.ADMIN,
        )

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
        now = datetime.now(timezone.utc).isoformat()
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
    ) -> None:
        """Mirror a post's media into the system album for the scope.

        Called by :class:`SystemAlbumBridge` on PostCreated / PostEdited
        / SpacePostCreated. Idempotent: an edit that doesn't change the
        media URLs is a no-op (skips churning rows + events).
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
        now = datetime.now(timezone.utc).isoformat()
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
