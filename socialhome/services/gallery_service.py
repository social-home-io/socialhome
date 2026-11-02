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
import hashlib
import io
import logging
import pathlib
import uuid
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import aiofiles
import aiofiles.os

from ..config import Config
from ..domain.errors import (
    CodedError,
    DownloadTooLargeError,
    PayloadTooLargeError,
    TranscodeQueueFullError,
    VideoTooLongError,
    VideoUnreadableError,
)
from ..domain.events import (
    GalleryAlbumCreated,
    GalleryAlbumDeleted,
    GalleryAlbumUpdated,
    GalleryItemDeleted,
    GalleryItemRestored,
    GalleryItemUpdated,
    GalleryItemUploaded,
    GalleryPrivateAlbumTrashed,
    UserDeprovisioned,
)
from ..domain.gallery import (
    AUDIENCE_CHOSEN,
    AUDIENCE_EVERYONE,
    AUDIENCE_MAX_PEOPLE,
    AUDIENCES,
    DOWNLOAD_MAX_BYTES,
    DOWNLOAD_MAX_ITEMS,
    GALLERY_MAX_UPLOAD_BYTES,
    GALLERY_VIDEO_MAX_QUEUED,
    GALLERY_VIDEO_MAX_SECONDS,
    GALLERY_VIDEO_MAX_UPLOAD_BYTES,
    PERSON_DOWNLOAD_MAX_ITEMS,
    TRASH_RETENTION_DAYS,
    GalleryAlbum,
    GalleryAlbumAudience,
    GalleryDownloadFile,
    GalleryItem,
    GallerySettings,
    GalleryTrashItem,
    GalleryUpload,
    ItemCursor,
    ItemSort,
    LockedTrashAlbum,
    StashedUpload,
    UploadLimits,
    UploadOutcome,
    is_video_upload,
    valid_taken_day,
    video_fits,
)
from ..domain.media_constraints import (
    CAPTION_MAX,
    VIDEO_MAX_DURATION_SECONDS,
)
from ..domain.media_transcode import VideoProbe
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
from ..media.cleanup import media_basename, unlink_unreferenced
from ..media.image_processor import ImageProcessor
from ..media.video_processor import VideoProcessor
from ..repositories.gallery_repo import AbstractGalleryRepo
from ..repositories.instance_config_repo import AbstractInstanceConfigRepo
from ..repositories.media_reference_repo import AbstractMediaReferenceRepo
from ..repositories.media_transcode_repo import AbstractMediaTranscodeRepo
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.user_repo import AbstractUserRepo
from .gallery_download import download_names
from .media_transcode_service import MediaTranscodeService
from .space_member_guard import NOT_A_SPACE_MEMBER, seat_of
from .storage_quota_service import StorageQuotaService
from .user_preferences import GALLERY_SEEN_KEY, parse_gallery_seen

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
#: One selection action (delete / restore / move) touches at most this many.
BULK_MAX_ITEMS: int = 500
BULK_ACTIONS: frozenset[str] = frozenset({"delete", "restore", "purge", "move"})
#: "Empty trash" removes at most this many per call (batch × batches).
EMPTY_TRASH_BATCH: int = 500
EMPTY_TRASH_MAX_BATCHES: int = 20
#: ``instance_config`` key holding the household default album's id.
#: ``instance_config`` key of the D6 switch "Kinder dürfen hochladen":
#: ``"0"`` when an admin turned it off; anything else (unset) is on.
CHILDREN_MAY_UPLOAD_KEY: str = "gallery.children_may_upload"

DEFAULT_ALBUM_KEY: str = "gallery.default_album_id"
#: The id of the default album once somebody renamed it. Until then each
#: viewer sees the default album under their own word for "Photos" (the
#: SPA localises it, ``default_name``), not the first uploader's. Holding
#: the album id (not a flag) means a new default album, made after the
#: renamed one was deleted, is localised again.
DEFAULT_ALBUM_RENAMED_KEY: str = "gallery.default_album_renamed"


# ─── Errors ──────────────────────────────────────────────────────────────


class _Unset:
    """An ``update_item`` field the request left out (``None`` clears it)."""

    __slots__ = ()


UNSET = _Unset()


def _item_patch(caption: str | None | _Unset, taken_at: str | None | _Unset) -> dict:
    """The validated ``caption`` / ``taken_at`` columns an item edit sets —
    a key only for a field the caller gave (``ValueError`` on a bad one)."""
    patch: dict = {}
    if not isinstance(caption, _Unset):
        text = (caption or "").strip()
        if len(text) > CAPTION_MAX:
            raise ValueError(f"Caption must be {CAPTION_MAX} characters or fewer")
        patch["caption"] = text or None
    if not isinstance(taken_at, _Unset):
        patch["taken_at"] = valid_taken_day(taken_at)
    return patch


class GalleryError(Exception):
    """Base error class for gallery operations."""


class GalleryNotFoundError(GalleryError):
    """Album or item not found."""


class GalleryPermissionError(GalleryError):
    """Caller is not allowed to perform this action."""


class AlbumAudienceError(CodedError, GalleryPermissionError):
    """A private-album change refused (D7) — 409 ``ALBUM_AUDIENCE`` with
    ``params.reason``:

    * ``would_hide`` — moving a photo into an album fewer people see would
      hide it from someone who sees it now (privacy follows the upload);
    * ``not_uploader`` — only the person who added a private photo may move
      it where more people see it;
    * ``not_empty`` — an album that already holds photos can't become
      private (it would hide them); make a new private album instead;
    * ``others_photos`` — a private album holding other people's photos
      can't be opened to everyone (they added them privately);
    * ``has_photos`` — someone who added photos to it can't be taken out
      (``params.user_id``);
    * ``default_album`` — the household's default album stays everyone's;
    * ``blocked`` — a guardian block separates the owner and someone they
      chose (``params.user_id``);
    * ``unknown_person`` — not an active person of this household;
    * ``too_many`` — over ``params.max`` people.
    """

    status = 409
    code = "ALBUM_AUDIENCE"
    detail = "That change to who sees this album isn't possible."

    def __init__(self, reason: str, **params: str | int) -> None:
        super().__init__(params={"reason": reason, **params})


class ChildUploadOffError(CodedError, GalleryPermissionError):
    """403 ``CHILD_UPLOAD_OFF``: the household turned "Children may upload"
    off (D6), and this is a protected account — it still sees the gallery,
    it adds nothing to the household's albums."""

    status = 403
    code = "CHILD_UPLOAD_OFF"
    detail = "Children can't add photos in this household right now."


# ─── Service ─────────────────────────────────────────────────────────────


class GalleryService:
    """CRUD + upload pipeline for gallery albums and items."""

    __slots__ = (
        "_bus",
        "_config",
        "_default_lock",
        "_instance_config",
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
        instance_config: AbstractInstanceConfigRepo | None = None,
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
        # Remembers which household album is the default one an upload
        # lands in when nobody picked an album (see
        # :meth:`ensure_default_album`). Without it there is no default.
        self._instance_config = instance_config
        # Two first uploads at once must not make two default albums.
        self._default_lock = asyncio.Lock()

    def wire(self) -> None:
        """Hand a removed person's household albums to an admin."""
        self._bus.subscribe(UserDeprovisioned, self._on_user_deprovisioned)

    # ─── Albums ───────────────────────────────────────────────────────────

    async def list_albums(
        self,
        *,
        space_id: str | None,
        actor_user_id: str,
        limit: int = 30,
        before: str | None = None,
        before_id: str | None = None,
    ) -> list[GalleryAlbum]:
        """One page of the albums in a space (or household) the actor can
        see — see :meth:`AbstractGalleryRepo.list_albums` for the order
        and the ``before`` / ``before_id`` cursor.

        Cover URL is enriched: explicit ``cover_item_id`` if set,
        else the first item's thumbnail.
        """
        if space_id is not None:
            await self._require_member(space_id, actor_user_id)
        # A household page holds the albums the viewer may see (D7); a
        # household admin's page also holds the private albums they aren't
        # in — as locked tiles they may delete, never open.
        admin = space_id is None and await self._is_household_admin(actor_user_id)
        albums = await self._repo.list_albums(
            space_id,
            limit=limit,
            before=before,
            before_id=before_id,
            by_activity=True,
            viewer_user_id=None if admin else actor_user_id,
        )
        marks = await self._default_marks() if space_id is None else (None, None)
        out: list[GalleryAlbum] = []
        for a in albums:
            if (
                admin
                and a.audience == AUDIENCE_CHOSEN
                and not await self._repo.album_visible_to(a.id, actor_user_id)
            ):
                out.append(_locked(a))
            else:
                out.append(await self._for_viewer(a, actor_user_id, marks=marks))
        return await self._with_new(out, actor_user_id)

    async def get_album(
        self,
        album_id: str,
        *,
        actor_user_id: str,
    ) -> GalleryAlbum:
        album = await self._visible_album(album_id, actor_user_id)
        [shown] = await self._with_new(
            [await self._for_viewer(album, actor_user_id)], actor_user_id, stamp=False
        )
        return shown

    async def visible_album(self, album_id: str, *, actor_user_id: str) -> GalleryAlbum:
        """The album as stored, for someone who may see it — else
        :class:`GalleryNotFoundError` (and 403 ``FEATURE_DISABLED`` for a
        space that switched its gallery off). No per-viewer decoration:
        the cheap gate for a by-id action on one of its items."""
        return await self._visible_album(album_id, actor_user_id)

    async def albums_as_seen(
        self, album_ids: list[str], *, actor_user_id: str
    ) -> dict[str, GalleryAlbum]:
        """The live albums of *album_ids* as the viewer sees them (cover,
        the default album's name) — the albums of a list that spans several,
        like the favourites. The caller already checked what the viewer may
        see; unknown or trashed ids are left out."""
        marks = await self._default_marks()
        out: dict[str, GalleryAlbum] = {}
        for album_id in dict.fromkeys(album_ids):
            album = await self._repo.get_album(album_id)
            if album is not None:
                out[album_id] = await self._for_viewer(
                    album, actor_user_id, marks=marks
                )
        return out

    async def mark_album_seen(self, album_id: str, *, actor_user_id: str) -> None:
        """The person opened the album: its "neu" dot (D9) goes until
        someone else adds a photo or video again. Kept in their
        ``preferences_json`` (:class:`GallerySeen`) — no table."""
        await self._visible_album(album_id, actor_user_id)
        if self._users is None:
            return
        user = await self._users.get_by_user_id(actor_user_id)
        if user is None:
            return
        seen = parse_gallery_seen(user.preferences_json)
        await self._users.set_preference_key(
            actor_user_id,
            GALLERY_SEEN_KEY,
            seen.opened(album_id, datetime.now(UTC)).to_dict(),
        )

    async def _with_new(
        self, albums: list[GalleryAlbum], viewer_user_id: str, *, stamp: bool = True
    ) -> list[GalleryAlbum]:
        """*albums* with :attr:`GalleryAlbum.has_new` set for the viewer
        (D9): someone else added a photo or video they may see since they
        last opened it. Never on the system "Posts" album — a feed post
        already tells people. The first album list someone reads only
        stamps when it started (:meth:`GallerySeen.stamped`, ``stamp``):
        nothing is new on the day the dot ships. Reading one album never
        writes."""
        if self._users is None or not albums:
            return albums
        user = await self._users.get_by_user_id(viewer_user_id)
        if user is None:
            return albums
        seen = parse_gallery_seen(user.preferences_json)
        if seen.since is None:
            if stamp:
                await self._users.set_preference_key(
                    viewer_user_id,
                    GALLERY_SEEN_KEY,
                    seen.stamped(datetime.now(UTC)).to_dict(),
                )
            return albums
        newest = await self._repo.newest_by_others(
            [a.id for a in albums if not a.is_system and not a.locked], viewer_user_id
        )
        return [
            replace(a, has_new=True) if seen.has_new(a.id, newest.get(a.id)) else a
            for a in albums
        ]

    async def _for_viewer(
        self,
        album: GalleryAlbum,
        viewer_user_id: str,
        *,
        marks: tuple[str | None, str | None] | None = None,
    ) -> GalleryAlbum:
        """The album as *viewer_user_id* sees it: its cover, whether it is
        the household's default album (still under its default name), and
        — for the system "Posts" album, which hides the posts of people the
        viewer blocked — the count of what they actually see, so the card
        never says 12 while 11 show. *marks* is :meth:`_default_marks`,
        read once for a whole list."""
        cover_url = await self._resolve_cover(album, viewer_user_id=viewer_user_id)
        shown = _with_cover(album, cover_url)
        if album.space_id is None and not album.is_system:
            default_id, renamed_id = (
                marks if marks is not None else await self._default_marks()
            )
            if album.id == default_id:
                shown = replace(
                    shown, is_default=True, default_name=renamed_id != album.id
                )
        if album.is_system:
            shown = replace(
                shown,
                item_count=await self._repo.count_visible_items(
                    album.id, viewer_user_id
                ),
            )
        return shown

    async def create_album(
        self,
        *,
        space_id: str | None,
        owner_user_id: str,
        name: str,
        description: str | None = None,
        audience: str = AUDIENCE_EVERYONE,
        user_ids: frozenset[str] = frozenset(),
    ) -> GalleryAlbum:
        """A new album. A household album may start private (``audience``
        ``chosen`` + *user_ids*, D7): everything uploaded into it is then
        seen only by those people, the owner and — for a protected owner —
        their guardians."""
        if audience not in AUDIENCES:
            raise ValueError("audience must be everyone or chosen")
        if audience == AUDIENCE_CHOSEN and space_id is not None:
            raise ValueError("a space album is seen by the whole space")
        if not name or not name.strip():
            raise ValueError(f"Album name must be 1–{NAME_MAX} characters")
        if len(name) > NAME_MAX:
            raise ValueError(f"Album name must be 1–{NAME_MAX} characters")
        if description and len(description) > DESCRIPTION_MAX:
            raise ValueError(
                f"Description must be {DESCRIPTION_MAX} characters or fewer"
            )
        if space_id is None:
            await self._require_may_add(owner_user_id)
        people: frozenset[str] = frozenset()
        if audience == AUDIENCE_CHOSEN:
            people = await self._valid_audience(owner_user_id, user_ids)
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
            audience=audience,
        )
        # A private album is stored with its people in one go: it is never
        # listed to everyone for a moment.
        await self._repo.create_album(album, audience_user_ids=people)
        await self._bus.publish(
            GalleryAlbumCreated(
                album_id=album.id,
                space_id=space_id,
                owner_id=owner_user_id,
            )
        )
        return album

    async def get_default_album(self, *, actor_user_id: str) -> GalleryAlbum | None:
        """The household's default album — where an upload goes when
        nobody chose an album — or ``None`` before the first such upload
        (or after someone deleted it)."""
        album = await self._stored_default_album()
        if album is None:
            return None
        return await self._for_viewer(album, actor_user_id)

    async def ensure_default_album(
        self, *, actor_user_id: str, name: str
    ) -> tuple[GalleryAlbum, bool]:
        """The household's default album, made now under *name* (the
        uploader's own word for "Photos") when there is none yet.

        It is an ordinary household album — the uploader owns it, anyone
        may rename it, and deleting it just means the next upload without
        an album makes a fresh one. Only its id is remembered
        (``instance_config``), so no new table or column is needed.
        Returns ``(album, created)``."""
        if self._instance_config is None:
            raise RuntimeError("the default album needs the instance config repo")
        async with self._default_lock:
            album = await self._stored_default_album()
            created = album is None
            if album is None:
                album = await self.create_album(
                    space_id=None, owner_user_id=actor_user_id, name=name
                )
                await self._instance_config.set(DEFAULT_ALBUM_KEY, album.id)
        return await self._for_viewer(album, actor_user_id), created

    async def _default_marks(self) -> tuple[str | None, str | None]:
        """``(default album id, renamed default album id)`` from
        ``instance_config`` — ``None`` where unset."""
        if self._instance_config is None:
            return None, None
        return (
            await self._instance_config.get(DEFAULT_ALBUM_KEY) or None,
            await self._instance_config.get(DEFAULT_ALBUM_RENAMED_KEY) or None,
        )

    async def _stored_default_album(self) -> GalleryAlbum | None:
        if self._instance_config is None:
            return None
        album_id = await self._instance_config.get(DEFAULT_ALBUM_KEY)
        if not album_id:
            return None
        album = await self._repo.get_album(album_id)
        if album is None or album.space_id is not None or album.is_system:
            return None
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
            if "name" in patch and album.space_id is None:
                await self._remember_renamed_default(album_id)
            await self._bus.publish(
                GalleryAlbumUpdated(album_id=album_id, space_id=album.space_id)
            )

    async def _remember_renamed_default(self, album_id: str) -> None:
        """Someone named the default album: from now on everybody sees
        that name, not their own word for "Photos"."""
        if self._instance_config is None:
            return
        if await self._instance_config.get(DEFAULT_ALBUM_KEY) == album_id:
            await self._instance_config.set(DEFAULT_ALBUM_RENAMED_KEY, album_id)

    async def delete_album(self, album_id: str, *, actor_user_id: str) -> None:
        album = await self._repo.get_album(album_id)
        if album is None:
            return
        # A household admin may put a private album they can't open in the
        # trash (D7: they see it as a locked tile) — its owner is told.
        locked = (
            album.space_id is None
            and album.audience == AUDIENCE_CHOSEN
            and await self._is_household_admin(actor_user_id)
            and not await self._can_see(album, actor_user_id)
        )
        if not locked and not await self._can_see(album, actor_user_id):
            # Idempotent, and an album of a space (or a private album) the
            # caller can't see answers exactly like an unknown id.
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
        if album.space_id is None:
            # A household album goes to the trash with its photos (30 days,
            # each restorable; a restore brings the album back). The files
            # stay until the purge.
            trashed = await self._repo.trash_album(album_id, deleted_by=actor_user_id)
            if (
                album.audience == AUDIENCE_CHOSEN
                and album.owner_user_id
                and album.owner_user_id != actor_user_id
            ):
                # Only an admin deletes someone else's album: its owner is
                # told, so nobody's private album disappears silently.
                await self._bus.publish(
                    GalleryPrivateAlbumTrashed(
                        album_id=album_id,
                        owner_id=album.owner_user_id,
                        admin_user_id=actor_user_id,
                        count=len(trashed),
                    )
                )
        else:
            media = await self._repo.list_album_media(album_id)
            # A space album keeps a tombstone (§25.6
            # ``gallery_albums_deleted``) — no trash: the delete goes out to
            # the member households.
            await self._repo.delete_album(album_id, deleted_by=actor_user_id)
            # Its items are file-free tombstones now: drop their files unless
            # another row still names them (same rule as ``delete_item``).
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
        before_id: str | None = None,
        sort: ItemSort = ItemSort.ADDED,
        after: ItemCursor | None = None,
    ) -> list[GalleryItem]:
        """One page of an album: newest upload first, or (*sort*) by the
        day taken — see :meth:`AbstractGalleryRepo.list_items` for the
        order and the cursors.

        The system "Posts" album mirrors the feed, so it hides what the
        feed hides: the posts of people the viewer blocked, or is
        guardian-blocked from."""
        album = await self._visible_album(album_id, actor_user_id)
        return await self._repo.list_items(
            album_id,
            limit=limit,
            before=before,
            before_id=before_id,
            viewer_user_id=actor_user_id if album.is_system else None,
            sort=sort,
            after=after,
        )

    async def upload_limits(
        self,
        album_id: str,
        *,
        uploader_user_id: str,
        declared_bytes: int | None = None,
    ) -> UploadLimits:
        """The largest files *album_id* takes from this person.

        Asked before a byte of the body is read, so a refusal (an album
        the person can't see or add to, a full disk for the declared
        size) costs nothing — a 2 GB video isn't sent to be turned away.
        """
        album = await self._album_for_upload(album_id, uploader_user_id, None)
        if declared_bytes and self._storage_quota is not None:
            await self._storage_quota.check_can_store(declared_bytes)
        if album.space_id is None:
            return UploadLimits(
                photo_bytes=GALLERY_MAX_UPLOAD_BYTES,
                video_bytes=GALLERY_VIDEO_MAX_UPLOAD_BYTES,
                video_seconds=GALLERY_VIDEO_MAX_SECONDS,
            )
        return UploadLimits(
            photo_bytes=GALLERY_MAX_UPLOAD_BYTES,
            video_bytes=GALLERY_MAX_UPLOAD_BYTES,
            video_seconds=VIDEO_MAX_DURATION_SECONDS,
        )

    async def stash_upload(
        self, chunks: AsyncIterable[bytes], *, max_bytes: int
    ) -> StashedUpload:
        """Write an upload to the transcode temp dir as it streams in.

        Never holds the file in memory: chunks are gathered into a few
        MiB, written with ``aiofiles`` and hashed (SHA-256, for the
        duplicate check) in a worker thread. Over *max_bytes* raises
        :class:`PayloadTooLargeError` (413); on any failure — a dropped
        connection included — the partial file is removed.
        """
        temp_dir = self._media_dir / "transcode_src"
        await aiofiles.os.makedirs(temp_dir, exist_ok=True)
        path = temp_dir / f"{uuid.uuid4().hex}.bin"
        digest = hashlib.sha256()
        size = 0
        pending = bytearray()
        try:
            async with aiofiles.open(path, "wb") as f:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > max_bytes:
                        raise PayloadTooLargeError(max_bytes)
                    pending += chunk
                    if len(pending) >= _STASH_FLUSH_BYTES:
                        await _write_and_hash(f, digest, bytes(pending))
                        pending.clear()
                if pending:
                    await _write_and_hash(f, digest, bytes(pending))
        except BaseException:
            await _remove_quietly(path)
            raise
        return StashedUpload(path=str(path), size=size, sha256=digest.hexdigest())

    async def upload_item(
        self,
        album_id: str,
        *,
        data: bytes,
        content_type: str,
        caption: str | None,
        uploader_user_id: str,
    ) -> GalleryUpload:
        """Process and store one photo or video.

        Photo path runs the ImageProcessor → WebP (longest side ≤
        ``IMAGE_MAX_DIMENSION``), extracts the EXIF date (day-only, no
        GPS), generates a WebP thumbnail.

        A video is written to the transcode temp dir and goes on as
        :meth:`upload_video` does (the HTTP route streams a video straight
        to disk with :meth:`stash_upload` instead of passing its bytes).

        A household upload is first checked for an exact duplicate: the
        SHA-256 of the bytes as uploaded (before conversion — every stored
        file is a fresh re-encode, so only the original can match) against
        the household photos the uploader can see. A live match stores
        nothing (:attr:`UploadOutcome.ALREADY_HERE`); a match in the trash
        comes back (:attr:`UploadOutcome.RESTORED`). Only the digest is
        kept, never the uploaded bytes. A space upload is not checked.
        """
        album = await self._album_for_upload(album_id, uploader_user_id, caption)
        if not data:
            raise ValueError("upload data is empty")
        if is_video_upload(content_type):
            stash = await self.stash_upload(_one_chunk(data), max_bytes=len(data))
            return await self._upload_video(
                album,
                stash=stash,
                caption=caption,
                uploader_user_id=uploader_user_id,
            )
        content_sha256: str | None = None
        if album.space_id is None:
            content_sha256 = await asyncio.to_thread(_sha256_hex, data)
            found = await self._find_duplicate(content_sha256, uploader_user_id)
            if found is not None:
                return found
        if self._storage_quota is not None:
            # 507 STORAGE_FULL via ``BaseView._iter``, before any disk write.
            await self._storage_quota.check_can_store(len(data))
        item = await self._upload_photo(
            album_id=album_id,
            space_id=album.space_id,
            data=data,
            content_type=content_type,
            caption=caption,
            uploader_user_id=uploader_user_id,
        )
        return await self._store_new_item(album, item, content_sha256)

    async def upload_video(
        self,
        album_id: str,
        *,
        stash: StashedUpload,
        caption: str | None,
        uploader_user_id: str,
    ) -> GalleryUpload:
        """Queue a video already written to disk (:meth:`stash_upload`).

        Reads the file's length, picture size and recording day
        (:meth:`VideoProcessor.probe_file`), refuses a household video
        longer than ``GALLERY_VIDEO_MAX_SECONDS`` (422 ``VIDEO_TOO_LONG``
        with its length — never cut), then queues the conversion and
        returns at once with the item's real size, length and day. A
        space video keeps the protocol's 60 s, as before. The stashed
        file is removed whenever the upload doesn't go ahead.
        """
        try:
            album = await self._album_for_upload(album_id, uploader_user_id, caption)
        except BaseException:
            await _remove_quietly(pathlib.Path(stash.path))
            raise
        return await self._upload_video(
            album,
            stash=stash,
            caption=caption,
            uploader_user_id=uploader_user_id,
        )

    async def album_for_copy(
        self, album_id: str, *, actor_user_id: str
    ) -> GalleryAlbum:
        """The space album a shared copy goes into (gallery D8), once this
        person may add to it — the upload rules: a seat that writes, the
        space's gallery on, never the system "Posts" album."""
        album = await self._album_for_upload(album_id, actor_user_id, None)
        if album.space_id is None:
            raise GalleryPermissionError("a copy goes into a space album")
        return album

    async def add_copy(self, album: GalleryAlbum, item: GalleryItem) -> GalleryItem:
        """Store *item* — a shared copy whose files are already on disk, in
        the stored format (WebP ≤ 2560 px / WebM ≤ 1920 px) — in *album*
        (from :meth:`album_for_copy`). It gets a fresh owner-bound id and
        goes out like any upload (``GalleryItemUploaded`` → the members
        of the space)."""
        copy = replace(
            item,
            id=_item_id(album.space_id, item.uploaded_by),
            album_id=album.id,
            sort_order=0,
            source_post_id=None,
            created_at=datetime.now(UTC).isoformat(),
        )
        stored = await self._store_new_item(album, copy, None)
        return stored.item

    async def _album_for_upload(
        self, album_id: str, uploader_user_id: str, caption: str | None
    ) -> GalleryAlbum:
        """The album, once this person may add (captioned) items to it."""
        album = await self._visible_album(album_id, uploader_user_id)
        if album.is_system:
            raise GalleryPermissionError(
                "system album cannot be uploaded to directly — "
                "share a photo or video via a feed post instead",
            )
        if album.space_id is not None:
            await self._require_writer(album.space_id, uploader_user_id)
        else:
            await self._require_may_add(uploader_user_id)
            if not await self._admitted(album.id, uploader_user_id):
                # They see it only through photos they added before (e.g. a
                # guardian whose child grew up): they keep those, add nothing.
                raise GalleryPermissionError("Only the album's people add to it")
        if caption and len(caption) > CAPTION_MAX:
            raise ValueError(f"Caption must be {CAPTION_MAX} characters or fewer")
        return album

    async def _find_duplicate(
        self, content_sha256: str, uploader_user_id: str
    ) -> GalleryUpload | None:
        """An upload's household duplicate the uploader can see, if any —
        brought back from the trash when it waits there."""
        duplicate = await self._repo.find_household_duplicate(
            content_sha256, viewer_user_id=uploader_user_id
        )
        if duplicate is None:
            return None
        if not duplicate.in_trash:
            return GalleryUpload(duplicate.item, UploadOutcome.ALREADY_HERE)
        restored = await self._restore(duplicate.item.id)
        if restored is not None:
            return GalleryUpload(restored, UploadOutcome.RESTORED)
        return None

    async def _store_new_item(
        self, album: GalleryAlbum, item: GalleryItem, content_sha256: str | None
    ) -> GalleryUpload:
        await self._repo.create_item(item, content_sha256=content_sha256)
        await self._repo.increment_item_count(album.id, +1)
        if album.space_id is None and not await self._admitted(
            album.id, item.uploaded_by
        ):
            # The album turned private without this person while the upload
            # was on its way (D7: privacy follows the upload) — it must not
            # land there: hidden from the others who saw it, and (since an
            # uploader always sees the album their photo is in) a way into
            # the private album. Writes are serialised: an audience change
            # after the insert sees this uploader and is refused, so this
            # check after the insert closes the race.
            await self._repo.trash_item(item.id, deleted_by=item.uploaded_by)
            media = await self._repo.purge_trashed_item(item.id)
            await unlink_unreferenced(self._media_dir, self._media_refs, media)
            raise GalleryNotFoundError(f"album {album.id!r} not found")
        await self._bus.publish(
            GalleryItemUploaded(
                item_id=item.id,
                album_id=album.id,
                item_type=item.item_type,
                uploader=item.uploaded_by,
                space_id=album.space_id,
            )
        )
        return GalleryUpload(item)

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
            else:
                await self._require_household_item_manager(
                    item.uploaded_by, actor_user_id
                )
        if album is not None and album.space_id is None:
            # A household photo goes to the trash for 30 days, files kept
            # (a restore brings it back; the purge removes it for good).
            if await self._repo.trash_item(item_id, deleted_by=actor_user_id):
                await self._bus.publish(
                    GalleryItemDeleted(item_id=item_id, album_id=item.album_id)
                )
            return
        # A space item keeps a file-free tombstone (§25.6
        # ``gallery_items_deleted``) — no trash: the delete goes out to the
        # member households.
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

    # ─── Trash (household only, migration 0139) ──────────────────────────
    #
    # A deleted household photo or video keeps its row and files for
    # ``TRASH_RETENTION_DAYS``; ``GalleryTrashPurgeScheduler`` then removes
    # it for good. Spaces have no trash: a space delete goes out to the
    # member households as before.

    async def list_trash(
        self,
        *,
        actor_user_id: str,
        limit: int = 100,
        before_id: str | None = None,
    ) -> list[GalleryTrashItem]:
        """The household trash *actor_user_id* may see (items of the albums
        they can see), last deleted first; ``before_id`` pages on."""
        return await self._repo.list_trash(
            viewer_user_id=actor_user_id, limit=limit, before_id=before_id
        )

    async def list_locked_trash(self, *, actor_user_id: str) -> list[LockedTrashAlbum]:
        """For a household admin: one locked tile per private album they
        aren't in that has photos in the trash (D7) — whose, how many, when;
        never a name or a preview. Anyone else: nothing (they can't act on
        it, and its existence is not theirs to know)."""
        if not await self._is_household_admin(actor_user_id):
            return []
        return await self._repo.list_locked_trash(actor_user_id)

    async def restore_item(self, item_id: str, *, actor_user_id: str) -> GalleryItem:
        """Bring a photo or video back from the trash — into its album,
        which comes back with it when it was deleted too. Whoever may
        delete the item may restore it."""
        trashed = await self._visible_trashed_item(item_id, actor_user_id)
        await self._require_household_item_manager(
            trashed.item.uploaded_by, actor_user_id
        )
        restored = await self._restore(item_id)
        if restored is None:  # purged or restored by someone else meanwhile
            raise GalleryNotFoundError(f"item {item_id!r} not found")
        return restored

    async def purge_trashed_item(self, item_id: str, *, actor_user_id: str) -> None:
        """Delete a photo or video in the trash for good, now."""
        trashed = await self._visible_trashed_item(item_id, actor_user_id)
        await self._require_household_item_manager(
            trashed.item.uploaded_by, actor_user_id
        )
        media = await self._repo.purge_trashed_item(item_id)
        await unlink_unreferenced(self._media_dir, self._media_refs, media)

    async def purge_expired_trash(
        self,
        *,
        now: datetime | None = None,
        batch: int = 500,
        max_batches: int = 20,
    ) -> int:
        """Remove what was trashed more than ``TRASH_RETENTION_DAYS`` ago
        (and the trashed albums left empty), with the files no other row
        names. Bounded per call (``batch`` × ``max_batches`` items); the
        next tick continues. Returns the media references released."""
        cutoff = (
            (now or datetime.now(UTC)) - timedelta(days=TRASH_RETENTION_DAYS)
        ).strftime("%Y-%m-%d %H:%M:%S")
        released = 0
        for _ in range(max_batches):
            media = await self._repo.purge_expired_trash(cutoff, limit=batch)
            if not media:
                break
            await unlink_unreferenced(self._media_dir, self._media_refs, media)
            released += len(media)
        return released

    async def _visible_trashed_item(
        self, item_id: str, actor_user_id: str
    ) -> GalleryTrashItem:
        """A trashed household item the actor may see — else the unknown
        id's 404, so nobody learns of a private album's photo."""
        trashed = await self._repo.get_trashed_item(
            item_id, viewer_user_id=actor_user_id
        )
        if trashed is None:
            raise GalleryNotFoundError(f"item {item_id!r} not found")
        return trashed

    async def _restore(self, item_id: str) -> GalleryItem | None:
        album_id = await self._repo.restore_item(item_id)
        if album_id is None:
            return None
        await self._bus.publish(GalleryItemRestored(item_id=item_id, album_id=album_id))
        return await self._repo.get_item(item_id)

    async def _require_household_item_manager(
        self, uploaded_by: str, actor_user_id: str
    ) -> None:
        """A single household photo is deleted, restored, purged or edited by
        its uploader or a household admin (owner decision D6, 2026-11-02) —
        a protected account (a child) only its own. The album's owner has no
        say over someone else's photo one by one; they may delete their
        album as a whole (:meth:`delete_album`), and its photos then wait in
        the trash, restorable by their uploaders."""
        if uploaded_by == actor_user_id:
            return
        if await self._is_protected(actor_user_id):
            raise GalleryPermissionError(
                "A protected account may only delete its own uploads"
            )
        if await self._is_household_admin(actor_user_id):
            return
        raise GalleryPermissionError(
            "Only the person who added it or a household admin may change this item"
        )

    async def restore_album(self, album_id: str, *, actor_user_id: str) -> int:
        """Bring a household album back from the trash with the photos that
        went there with it — the Undo of an album delete. Whoever may delete
        the album may restore it. How many photos came back."""
        album = await self._repo.get_trashed_album(album_id)
        if album is None:
            raise GalleryNotFoundError(f"album {album_id!r} not found")
        await self._require_album_owner_or_admin(album, actor_user_id)
        restored = await self._repo.restore_album(album_id)
        # One frame for the album, not one per photo: the album list, the
        # open album and the trash all refetch on it.
        await self._bus.publish(GalleryAlbumUpdated(album_id=album_id, space_id=None))
        return len(restored)

    async def empty_trash(self, *, actor_user_id: str) -> int:
        """A household admin deletes everything in the trash they can see
        for good, now. Bounded per call like the purge; how many went."""
        if not await self._is_household_admin(actor_user_id):
            raise GalleryPermissionError("Only a household admin may empty the trash")
        removed = 0
        for _ in range(EMPTY_TRASH_MAX_BATCHES):
            rows = await self._repo.list_trash(
                viewer_user_id=actor_user_id, limit=EMPTY_TRASH_BATCH
            )
            if not rows:
                break
            for row in rows:
                media = await self._repo.purge_trashed_item(row.item.id)
                await unlink_unreferenced(self._media_dir, self._media_refs, media)
                removed += 1
        return removed

    # ─── Managing: caption, date, album ──────────────────────────────────

    async def update_item(
        self,
        item_id: str,
        *,
        actor_user_id: str,
        caption: str | None | _Unset = UNSET,
        taken_at: str | None | _Unset = UNSET,
        album_id: str | _Unset = UNSET,
    ) -> GalleryItem:
        """Change a photo's caption, its date taken or its album (a photo
        lives in exactly one album). Whoever may delete it may change it —
        in the household the uploader or a household admin (D6), in a space
        the uploader or a space owner / admin / moderator (v_79, the change
        federates as ``SPACE_GALLERY_ITEM_UPDATED``); a protected account
        only its own. A space photo moves only between albums of its own
        space."""
        item = await self._repo.get_item(item_id)
        album = await self._repo.get_album(item.album_id) if item else None
        if (
            item is None
            or album is None
            or not await self._can_see(album, actor_user_id)
        ):
            raise GalleryNotFoundError(f"item {item_id!r} not found")
        if album.is_system:
            raise GalleryPermissionError(
                "system album items are managed automatically — edit the post"
            )
        if album.space_id is not None:
            return await self._update_space_item(
                item,
                album,
                actor_user_id,
                caption=caption,
                taken_at=taken_at,
                album_id=album_id,
            )
        await self._require_household_item_manager(item.uploaded_by, actor_user_id)
        patch = _item_patch(caption, taken_at)
        from_album: str | None = None
        if not isinstance(album_id, _Unset) and album_id != item.album_id:
            target = await self._repo.get_album(str(album_id))
            if (
                target is None
                or target.space_id is not None
                or not await self._can_see(target, actor_user_id)
            ):
                raise GalleryNotFoundError(f"album {album_id!r} not found")
            if target.is_system:
                raise GalleryPermissionError(
                    "nothing can be moved into the feed's Posts album"
                )
            await self._check_move(item, album, target, actor_user_id)
            from_album = await self._repo.move_item(item_id, target.id)
            if from_album is None:
                raise GalleryNotFoundError(f"item {item_id!r} not found")
        if patch:
            await self._repo.update_item(item_id, patch)
        updated = await self._repo.get_item(item_id)
        if updated is None:  # trashed meanwhile
            raise GalleryNotFoundError(f"item {item_id!r} not found")
        if patch or from_album is not None:
            await self._bus.publish(
                GalleryItemUpdated(
                    item_id=item_id,
                    album_id=updated.album_id,
                    from_album_id=from_album,
                    actor_user_id=actor_user_id,
                )
            )
        return updated

    async def _update_space_item(
        self,
        item: GalleryItem,
        album: GalleryAlbum,
        actor_user_id: str,
        *,
        caption: str | None | _Unset,
        taken_at: str | None | _Unset,
        album_id: str | _Unset,
    ) -> GalleryItem:
        """:meth:`update_item` for a space photo (v_79). A change is a write
        (a follower gets ``SUBSCRIBER_READ_ONLY``); the uploader or content
        authority (owner / admin / moderator) may make it — the delete rule
        of :meth:`delete_item`. Only a real change is published, so an
        unchanged save sends nothing to the member households."""
        space_id = str(album.space_id)
        await self._require_gallery_on(album)
        await self._require_writer(space_id, actor_user_id)
        if item.uploaded_by != actor_user_id:
            if await self._is_protected(actor_user_id):
                raise GalleryPermissionError(
                    "A protected account may only change its own uploads"
                )
            if not await self._has_content_authority(space_id, actor_user_id):
                raise GalleryPermissionError(
                    "Only the uploader or a space moderator may change this item"
                )
        patch = _item_patch(caption, taken_at)
        to_album: str | None = None
        if not isinstance(album_id, _Unset) and album_id != item.album_id:
            target = await self._repo.get_album(str(album_id))
            if target is None or target.space_id != space_id:
                # Another space's album, or the household's: never a move
                # target — a space photo stays in its space.
                raise GalleryNotFoundError(f"album {album_id!r} not found")
            if target.is_system:
                raise GalleryPermissionError(
                    "nothing can be moved into the space's Posts album"
                )
            to_album = target.id
        changed = {k: v for k, v in patch.items() if getattr(item, k) != v}
        if not changed and to_album is None:
            return item
        from_album = await self._repo.edit_item_in_space(
            item.id, space_id=space_id, patch=changed, to_album_id=to_album
        )
        updated = await self._repo.get_item(item.id)
        if from_album is None or updated is None:  # deleted / moved meanwhile
            raise GalleryNotFoundError(f"item {item.id!r} not found")
        await self._bus.publish(
            GalleryItemUpdated(
                item_id=item.id,
                album_id=updated.album_id,
                from_album_id=from_album if updated.album_id != from_album else None,
                space_id=space_id,
                actor_user_id=actor_user_id,
            )
        )
        return updated

    async def apply_to_items(
        self,
        action: str,
        item_ids: list[str],
        *,
        actor_user_id: str,
        album_id: str | None = None,
    ) -> tuple[list[str], list[str]]:
        """One action for a selection — ``delete`` (a household photo to the
        trash, a space photo for good), ``restore`` (back from the trash —
        the Undo), ``purge`` (out of the trash for good) or ``move`` (to
        ``album_id``). Each photo is checked on
        its own, exactly as the single-item route would: the ones the actor
        may not touch are refused, the rest done. ``(done, refused)``."""
        if action not in BULK_ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        ids = list(dict.fromkeys(str(i) for i in item_ids if i))
        if not ids or len(ids) > BULK_MAX_ITEMS:
            raise ValueError(f"1–{BULK_MAX_ITEMS} items per request")
        if action == "move" and not album_id:
            raise ValueError("move needs an album_id")
        done: list[str] = []
        refused: list[str] = []
        for iid in ids:
            try:
                if action == "delete":
                    await self.delete_item(iid, actor_user_id=actor_user_id)
                elif action == "restore":
                    await self.restore_item(iid, actor_user_id=actor_user_id)
                elif action == "purge":
                    await self.purge_trashed_item(iid, actor_user_id=actor_user_id)
                else:
                    await self.update_item(
                        iid, actor_user_id=actor_user_id, album_id=str(album_id)
                    )
            except (
                GalleryPermissionError,
                GalleryNotFoundError,
                SubscriberReadOnlyError,
                FeatureDisabledError,
            ):
                refused.append(iid)
            else:
                done.append(iid)
        return done, refused

    # ─── Downloads (stored WebP / WebM, human file names) ────────────────

    async def download_files(
        self,
        item_ids: list[str],
        *,
        actor_user_id: str,
        system_album_label: str | None = None,
    ) -> list[GalleryDownloadFile]:
        """The stored files of the photos and videos the actor picked and may
        see, named for people (:func:`download_names`). Unknown ids, items
        of albums the actor can't see and videos still transcoding are left
        out. Over :data:`DOWNLOAD_MAX_ITEMS` or :data:`DOWNLOAD_MAX_BYTES`
        is refused before anything is read."""
        ids = list(dict.fromkeys(str(i) for i in item_ids if i))
        if not ids:
            raise ValueError("no items to download")
        if len(ids) > DOWNLOAD_MAX_ITEMS:
            raise DownloadTooLargeError(DOWNLOAD_MAX_ITEMS, DOWNLOAD_MAX_BYTES)
        albums: dict[str, GalleryAlbum | None] = {}
        picked: list[tuple[GalleryItem, str]] = []
        for iid in ids:
            item = await self._repo.get_item(iid)
            if item is None:
                continue
            if item.album_id not in albums:
                found = await self._repo.get_album(item.album_id)
                if found is not None and not await self._can_see(found, actor_user_id):
                    found = None
                if found is not None:
                    await self._require_gallery_on(found)
                albums[item.album_id] = found
            album = albums[item.album_id]
            if album is None:
                continue
            label = album.name
            if album.is_system and system_album_label:
                label = system_album_label
            picked.append((item, label))
        return await self._stored_files(
            picked, max_items=DOWNLOAD_MAX_ITEMS, max_bytes=DOWNLOAD_MAX_BYTES
        )

    async def person_uploads(
        self, username: str, *, actor_user_id: str
    ) -> tuple[str, list[GalleryItem], dict[str, str]]:
        """A person's household uploads, for the leaving-household ZIP: the
        person themselves or a household admin asks. Only what the actor
        may see is in it (an admin never gets a private album's photos).
        ``(display name, items, album names by id)``."""
        person = await self._users.get(username) if self._users else None
        if person is None:
            raise GalleryNotFoundError("person not found")
        if person.user_id != actor_user_id and not await self._is_household_admin(
            actor_user_id
        ):
            raise GalleryPermissionError(
                "Only the person or a household admin may download their photos"
            )
        # Only what the actor may see, filtered before the limit — so no
        # photo they may see drops because hidden ones came first.
        items = await self._repo.list_household_uploads(
            person.user_id,
            limit=PERSON_DOWNLOAD_MAX_ITEMS + 1,
            viewer_user_id=actor_user_id,
        )
        names: dict[str, str] = {}
        visible: list[GalleryItem] = []
        for item in items:
            if item.album_id not in names:
                album = await self._repo.get_album(item.album_id)
                seen = album is not None and await self._can_see(album, actor_user_id)
                names[item.album_id] = album.name if seen and album else ""
            if names[item.album_id]:
                visible.append(item)
        return person.display_name or person.username, visible, names

    async def person_download_files(
        self, username: str, *, actor_user_id: str
    ) -> tuple[str, list[GalleryDownloadFile]]:
        """``(display name, files)`` of :meth:`person_uploads`."""
        name, items, albums = await self.person_uploads(
            username, actor_user_id=actor_user_id
        )
        if len(items) > PERSON_DOWNLOAD_MAX_ITEMS:
            raise DownloadTooLargeError(PERSON_DOWNLOAD_MAX_ITEMS, DOWNLOAD_MAX_BYTES)
        files = await self._stored_files(
            [(i, albums[i.album_id]) for i in items],
            max_items=PERSON_DOWNLOAD_MAX_ITEMS,
            max_bytes=DOWNLOAD_MAX_BYTES,
        )
        return name, files

    async def _stored_files(
        self,
        picked: list[tuple[GalleryItem, str]],
        *,
        max_items: int,
        max_bytes: int,
    ) -> list[GalleryDownloadFile]:
        """The files on disk behind *picked*, named, with their sizes — a
        file that isn't there (a video still transcoding) is left out."""
        names = download_names(picked)
        out: list[GalleryDownloadFile] = []
        total = 0
        for (item, _label), name in zip(picked, names, strict=True):
            base = media_basename(item.url)
            if base is None:
                continue
            path = self._media_dir / base
            if not await aiofiles.os.path.isfile(path):
                continue
            size = (await aiofiles.os.stat(path)).st_size
            total += size
            if total > max_bytes:
                raise DownloadTooLargeError(max_items, max_bytes)
            out.append(GalleryDownloadFile(path=str(path), name=name, size=size))
        return out

    # ─── Leaving the household ────────────────────────────────────────────

    async def _on_user_deprovisioned(self, event: UserDeprovisioned) -> None:
        """Keep a removed person's photos: their uploads stay where they are
        (no gallery row goes with its person — 0046 dropped those FKs), and
        the household albums they owned pass to an admin, so someone can
        still manage them. A private album stays theirs: handing it over
        would show an admin what its owner kept from them."""
        if self._users is None:
            return
        await self._trash_orphaned_private_albums(event.user_id)
        admins = sorted(
            (
                u
                for u in await self._users.list_active()
                if u.is_admin and u.user_id != event.user_id
            ),
            key=lambda u: u.username,
        )
        if not admins:
            log.warning(
                "gallery: a removed person's albums stay theirs — no admin left "
                "to take them over"
            )
            return
        moved = await self._repo.reassign_household_albums(
            event.user_id, admins[0].user_id
        )
        if moved:
            log.info(
                "gallery: %d album(s) of a removed person handed to an admin", moved
            )

    async def _trash_orphaned_private_albums(self, user_id: str) -> None:
        """A removed person's private albums that nobody else sees go to the
        trash (r3: never an ownerless locked tile forever) — 30 days, then
        the purge; one someone else sees stays, owned by them still."""
        for album_id in await self._repo.list_private_albums_of(user_id):
            audience = await self._repo.get_album_audience(album_id)
            viewers = audience.viewers if audience is not None else None
            if viewers is None or viewers - {user_id}:
                continue
            await self._repo.trash_album(album_id, deleted_by=user_id)
            await self._bus.publish(
                GalleryAlbumDeleted(album_id=album_id, space_id=None, owner_id=user_id)
            )

    # ─── Private albums (D7) ──────────────────────────────────────────────

    async def get_album_audience(
        self, album_id: str, *, actor_user_id: str
    ) -> GalleryAlbumAudience:
        """Who sees a household album, for someone who sees it."""
        album = await self._visible_album(album_id, actor_user_id)
        audience = (
            await self._repo.get_album_audience(album.id)
            if album.space_id is None
            else None
        )
        if audience is None:
            raise GalleryNotFoundError(f"album {album_id!r} not found")
        return audience

    async def set_album_audience(
        self,
        album_id: str,
        *,
        actor_user_id: str,
        audience: str,
        user_ids: frozenset[str] = frozenset(),
    ) -> GalleryAlbumAudience:
        """The album's owner chooses who sees it (D7) — never an admin, who
        can't open a private album. Privacy follows the upload, so a change
        never hides a photo from someone who saw it nor shows someone's
        private photo wider than they added it:

        * everyone → private: only while the album holds no photos;
        * private → everyone: only while every photo in it is the owner's;
        * private → private: anyone may be added; someone who added photos
          stays in (a protected owner's guardians are always in, outside
          the list — :data:`ALBUM_VISIBLE_TO_SQL`);
        * the household's default album stays everyone's.
        """
        if audience not in AUDIENCES:
            raise ValueError("audience must be everyone or chosen")
        album = await self._visible_album(album_id, actor_user_id)
        if album.space_id is not None or album.is_system:
            raise GalleryPermissionError("only a household album can be private")
        if album.owner_user_id != actor_user_id:
            raise GalleryPermissionError("Only the album's owner chooses who sees it")
        current = await self._repo.get_album_audience(album.id)
        if current is None:
            raise GalleryNotFoundError(f"album {album_id!r} not found")
        people: frozenset[str] = frozenset()
        if audience == AUDIENCE_CHOSEN:
            people = await self._valid_audience(actor_user_id, user_ids)
            default_id, _ = await self._default_marks()
            if album.id == default_id:
                raise AlbumAudienceError("default_album")
        uploaders = await self._repo.album_uploaders(album.id)
        # Who may have photos in the album once it changes — the same rule
        # again inside the write's transaction (``allowed_uploaders``), so a
        # photo landing meanwhile never ends up hidden or shown wider.
        if current.audience == AUDIENCE_EVERYONE and audience == AUDIENCE_CHOSEN:
            allowed: frozenset[str] = frozenset()
            if uploaders:
                raise AlbumAudienceError("not_empty")
        elif current.audience == AUDIENCE_CHOSEN and audience == AUDIENCE_EVERYONE:
            allowed = frozenset({actor_user_id})
            if uploaders - allowed:
                raise AlbumAudienceError("others_photos")
        elif audience == AUDIENCE_CHOSEN:
            allowed = people | current.guardian_ids | {actor_user_id}
            for gone in sorted(uploaders - allowed):
                raise AlbumAudienceError("has_photos", user_id=gone)
        else:
            allowed = uploaders | {actor_user_id}  # everyone → everyone
        if not await self._repo.set_album_audience(
            album.id, audience, people, allowed_uploaders=allowed
        ):
            raise AlbumAudienceError("not_empty" if not allowed else "has_photos")
        await self._bus.publish(GalleryAlbumUpdated(album_id=album.id, space_id=None))
        updated = await self._repo.get_album_audience(album.id)
        if updated is None:  # deleted meanwhile
            raise GalleryNotFoundError(f"album {album_id!r} not found")
        return updated

    async def _admitted(self, album_id: str, user_id: str) -> bool:
        """Whether *user_id* sees a household album by its audience — open
        to everyone, or its owner, chosen people or a protected owner's
        guardians — not merely because a photo of theirs is in it (the
        upload rule: nobody gets into a private album by adding to it)."""
        audience = await self._repo.get_album_audience(album_id)
        if audience is None:
            return False
        if audience.audience != AUDIENCE_CHOSEN:
            return True
        return (
            user_id == audience.owner_user_id
            or user_id in audience.user_ids
            or user_id in audience.guardian_ids
        )

    async def is_private_file(self, filename: str) -> bool:
        """Whether *filename* is a private album's photo, video or
        thumbnail — the file route then sends it ``no-store``."""
        return await self._repo.is_private_media(filename)

    async def may_open_file(self, filename: str, *, user_id: str) -> bool:
        """Whether *user_id* may fetch the stored file *filename* — not when
        it is a photo, video or thumbnail of a private album they can't see
        and didn't add (D7). Every other file is the media route's own
        business (its names are unguessable)."""
        return not await self._repo.media_hidden_from(filename, user_id)

    async def _valid_audience(
        self, owner_user_id: str, user_ids: frozenset[str]
    ) -> frozenset[str]:
        """The chosen people of a private album: active people of this
        household (the owner is implied), none a guardian block separates
        from the owner (§CP.F2), at most :data:`AUDIENCE_MAX_PEOPLE`."""
        people = frozenset(str(u) for u in user_ids if u) - {owner_user_id}
        if len(people) > AUDIENCE_MAX_PEOPLE:
            raise AlbumAudienceError("too_many", max=AUDIENCE_MAX_PEOPLE)
        if not people:
            return people
        if self._users is None:
            raise AlbumAudienceError("unknown_person")
        active = {u.user_id for u in await self._users.list_active()}
        if people - active:
            raise AlbumAudienceError("unknown_person")
        separated = await self._repo.guardian_block_counterparts(owner_user_id)
        for uid in sorted(people & separated):
            raise AlbumAudienceError("blocked", user_id=uid)
        return people

    async def _check_move(
        self,
        item: GalleryItem,
        source: GalleryAlbum,
        target: GalleryAlbum,
        actor_user_id: str,
    ) -> None:
        """Privacy follows the upload (D7). With one album per photo, moving
        a photo *is* changing who sees it, so a move:

        * never narrows: into an album that doesn't show it to everyone who
          sees it now → refused (``would_hide``). Moving a photo into a
          private album is therefore refused unless it already sits in a
          private album shown to no more people — the photo stays where
          everyone who saw it still does; to keep something private, upload
          it into the private album (nothing is copied, nothing hides);
        * widens only by its uploader: out of a private album into one more
          people see (``not_uploader``) — like sharing (D8).
        """
        before = await self._repo.get_album_audience(source.id)
        after = await self._repo.get_album_audience(target.id)
        if before is None or after is None:
            raise GalleryNotFoundError("album not found")
        seen_by, will_see = before.viewers, after.viewers
        if will_see is not None and (seen_by is None or not seen_by <= will_see):
            raise AlbumAudienceError("would_hide")
        widens = seen_by is not None and (will_see is None or will_see != seen_by)
        if widens and item.uploaded_by != actor_user_id:
            raise AlbumAudienceError("not_uploader")

    # ─── Children (D6) and the gallery's settings ─────────────────────────

    async def get_settings(self) -> GallerySettings:
        """The household's gallery rules (D6)."""
        if self._instance_config is None:
            return GallerySettings()
        raw = await self._instance_config.get(CHILDREN_MAY_UPLOAD_KEY)
        return GallerySettings(children_may_upload=raw != "0")

    async def set_settings(
        self, *, actor_user_id: str, children_may_upload: bool | None = None
    ) -> GallerySettings:
        """A household admin sets the gallery's rules."""
        if not await self._is_household_admin(actor_user_id):
            raise GalleryPermissionError("Only a household admin changes this")
        if self._instance_config is None:
            raise RuntimeError("gallery settings need the instance config repo")
        if children_may_upload is not None:
            await self._instance_config.set(
                CHILDREN_MAY_UPLOAD_KEY, "1" if children_may_upload else "0"
            )
        return await self.get_settings()

    async def may_add(self, user_id: str) -> bool:
        """Whether *user_id* may add photos and albums to the household
        gallery — everyone, except a child while "Children may upload" is
        off (D6)."""
        if not await self._is_protected(user_id):
            return True
        return (await self.get_settings()).children_may_upload

    async def _require_may_add(self, user_id: str) -> None:
        if not await self.may_add(user_id):
            raise ChildUploadOffError()

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
        album: GalleryAlbum,
        *,
        stash: StashedUpload,
        caption: str | None,
        uploader_user_id: str,
    ) -> GalleryUpload:
        """Check a stashed video, queue its conversion, store its item.

        Mints the eventual ``.webm`` output + ``.webp`` poster names from
        one UUID stem, enqueues a ``media_transcode_jobs`` row with the
        length to keep, and nudges the scheduler; the SPA shows a
        "processing" tile (with an estimate) until
        :meth:`MediaTranscodeService.flush_once` writes the files.

        Requires the transcode repo + service to be wired; without them
        there is no background worker to drain the queue, so we fail loud
        rather than silently dropping the upload.
        """
        source = pathlib.Path(stash.path)
        try:
            if self._transcode_repo is None or self._transcode_service is None:
                raise RuntimeError(
                    "video upload requires the media transcode service to be wired"
                )
            if stash.size == 0:
                raise ValueError("upload data is empty")
            household = album.space_id is None
            content_sha256 = stash.sha256 if household else None
            if content_sha256 is not None:
                found = await self._find_duplicate(content_sha256, uploader_user_id)
                if found is not None:
                    await _remove_quietly(source)
                    return found
            if self._storage_quota is not None:
                await self._storage_quota.check_can_store(stash.size)
            probe = await self._probe_video(source)
            keep_s = _video_keep_seconds(probe, household=household)
            if await self._transcode_repo.count_queued() >= GALLERY_VIDEO_MAX_QUEUED:
                raise TranscodeQueueFullError()
            # ONE shared UUID stem for the transcoded ``.webm`` and its
            # ``.webp`` poster so the poster path is derivable from the
            # media URL server-side (matches the feed/DM upload path).
            stem = uuid.uuid4().hex
            output_filename = f"{stem}.webm"
            thumbnail_filename = f"{stem}.webp"
            await self._transcode_repo.enqueue(
                output_filename=output_filename,
                source_path=str(source),
                thumbnail_filename=thumbnail_filename,
                owner_user_id=uploader_user_id,
                duration_s=keep_s,
            )
        except BaseException:
            await _remove_quietly(source)
            raise
        self._transcode_service.nudge()
        item = GalleryItem(
            id=_item_id(album.space_id, uploader_user_id),
            album_id=album.id,
            uploaded_by=uploader_user_id,
            item_type="video",
            url=f"api/media/{output_filename}",
            thumbnail_url=f"api/media/{thumbnail_filename}",
            width=probe.width,
            height=probe.height,
            duration_s=round(keep_s, 2),
            caption=caption,
            taken_at=probe.taken_on,
            sort_order=0,
            created_at=datetime.now(UTC).isoformat(),
        )
        return await self._store_new_item(album, item, content_sha256)

    async def _probe_video(self, source: pathlib.Path) -> VideoProbe:
        """The stashed video's length, size and day — or 422."""
        try:
            return await VideoProcessor().probe_file(source)
        except ValueError as exc:
            log.info("gallery: video upload refused, unreadable: %s", exc)
            raise VideoUnreadableError() from exc

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

    async def _resolve_cover(
        self, album: GalleryAlbum, *, viewer_user_id: str
    ) -> str | None:
        if album.cover_item_id:
            item = await self._repo.get_item(album.cover_item_id)
            # Only an item of this album: a cover id can name one that has
            # not arrived yet, or (from the wire) one filed elsewhere.
            if item is not None and item.album_id == album.id:
                return item.thumbnail_url
        # The system album's cover follows its listing: never a photo by
        # someone the viewer blocked.
        return await self._repo.get_first_item_thumbnail(
            album.id, viewer_user_id=viewer_user_id if album.is_system else None
        )

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
        """A household album: its audience (D7) — everyone at home, or for a
        private album its owner, chosen people and a protected owner's
        guardians: the repo's one rule
        (:meth:`AbstractGalleryRepo.album_visible_to`), which every list
        query shares. A space album: a seat in its space (:func:`seat_of` —
        not a banned user's leftover)."""
        if album.space_id is None:
            if album.audience != AUDIENCE_CHOSEN:
                return True
            return await self._repo.album_visible_to(album.id, user_id)
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
            await self._unlink_mirror_thumbnails(existing)
        now = datetime.now(UTC).isoformat()
        made: list[str] = []
        for url, item_type in urls:
            thumbnail_url = await self._mirror_thumbnail(url, item_type)
            if thumbnail_url != url and item_type == "photo":
                made.append(thumbnail_url)
            item = GalleryItem(
                id=uuid.uuid4().hex,
                album_id=album.id,
                uploaded_by=post.author,
                item_type=item_type,
                url=url,
                thumbnail_url=thumbnail_url,
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
        if made:
            # A concurrent mirror of the same post may have won the insert
            # (``ON CONFLICT DO NOTHING``): drop the thumbnails no row kept.
            kept = {
                it.thumbnail_url
                for it in await self._repo.list_items_by_source_post(post.id)
            }
            await unlink_unreferenced(
                self._media_dir, self._media_refs, [u for u in made if u not in kept]
            )

    async def _mirror_thumbnail(self, url: str, item_type: str) -> str:
        """The grid thumbnail for a mirrored post file (UX review #18).

        A photo gets a real thumbnail, made once from the post's file —
        the grid otherwise loads every full 2560 px image. A video uses
        the poster its transcode writes next to it (``<stem>.webp``). When
        the photo is not on disk yet (a space post whose media is still
        syncing) or can't be read, the full file stands in, as before.
        """
        name = media_basename(url)
        if name is None:
            return url
        if item_type == "video":
            stem, dot, _ext = name.rpartition(".")
            return f"api/media/{stem}.webp" if dot and stem else url
        path = self._media_dir / name
        try:
            if not await aiofiles.os.path.isfile(path):
                return url
            async with aiofiles.open(path, "rb") as f:
                data = await f.read()
            thumb = await ImageProcessor().generate_thumbnail(data)
        except OSError, ValueError:
            log.debug("gallery: mirror thumbnail failed for %s", name, exc_info=True)
            return url
        thumb_name = f"{uuid.uuid4().hex}.webp"
        await self._save_to_disk(thumb_name, thumb)
        return f"api/media/{thumb_name}"

    async def _unlink_mirror_thumbnails(self, items: list[GalleryItem]) -> None:
        """Drop the thumbnails :meth:`_mirror_thumbnail` made for *items* —
        never the post's own files (its photo, a video's poster): the
        post's delete path owns those."""
        await unlink_unreferenced(
            self._media_dir,
            self._media_refs,
            [
                it.thumbnail_url
                for it in items
                if it.item_type == "photo" and it.thumbnail_url != it.url
            ],
        )

    async def unmirror_post(self, post_id: str) -> None:
        """Remove every system-album item mirrored from ``post_id``.

        Called by :class:`SystemAlbumBridge` on PostDeleted /
        SpacePostModerated. The lookup is via the
        ``idx_gallery_items_source_post`` index — O(1) regardless of
        scope, so the bridge doesn't need to know whether the post
        lived in the household feed or a space.
        """
        mirrored = await self._repo.list_items_by_source_post(post_id)
        album_id, count = await self._repo.delete_items_by_source_post(post_id)
        if album_id is None or count == 0:
            return
        await self._unlink_mirror_thumbnails(mirrored)
        await self._repo.increment_item_count(album_id, -count)
        # No per-item GalleryItemDeleted events: the post-delete event
        # already drove this; emitting a flood of item-delete frames
        # here would only duplicate the WS notification.


# ─── Helpers ─────────────────────────────────────────────────────────────


def _locked(album: GalleryAlbum) -> GalleryAlbum:
    """A private album as a household admin who isn't in it sees it: a
    locked tile — whose, how many, never its name, description or cover."""
    return replace(
        album,
        name="",
        description=None,
        cover_item_id=None,
        cover_url=None,
        retention_exempt=False,
        is_default=False,
        default_name=False,
        has_new=False,
        locked=True,
    )


def _with_cover(album: GalleryAlbum, cover_url: str | None) -> GalleryAlbum:
    """Return a copy of *album* with ``cover_url`` filled in."""
    return replace(album, cover_url=cover_url)


#: :meth:`GalleryService.stash_upload` writes and hashes in batches of
#: this size (one worker-thread hop per batch, not per 64 KiB chunk).
_STASH_FLUSH_BYTES: int = 4 * 1024 * 1024


async def _write_and_hash(f, digest, data: bytes) -> None:
    await f.write(data)
    await asyncio.to_thread(digest.update, data)


async def _remove_quietly(path: pathlib.Path) -> None:
    """Remove a temp file; one that is already gone is fine."""
    try:
        await aiofiles.os.remove(path)
    except FileNotFoundError:
        return
    except OSError as exc:  # pragma: no cover — defensive
        log.warning("gallery: could not remove temp upload %s: %s", path, exc)


async def _one_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _video_keep_seconds(probe: VideoProbe, *, household: bool) -> float:
    """How much of a video is kept — or the refusal when it's too long.

    Household: the whole video, up to ``GALLERY_VIDEO_MAX_SECONDS``;
    longer is refused with its length (owner decision D4: never cut
    silently). A file that doesn't say how long it is keeps up to the
    limit. A space keeps the protocol's first 60 s, as before.
    """
    if household:
        if probe.duration_s is None:
            return float(GALLERY_VIDEO_MAX_SECONDS)
        if not video_fits(probe.duration_s, GALLERY_VIDEO_MAX_SECONDS):
            raise VideoTooLongError(probe.duration_s, GALLERY_VIDEO_MAX_SECONDS)
        return probe.duration_s
    if probe.duration_s is None:
        return float(VIDEO_MAX_DURATION_SECONDS)
    return min(probe.duration_s, float(VIDEO_MAX_DURATION_SECONDS))


def _sha256_hex(data: bytes) -> str:
    """SHA-256 of the uploaded bytes — run off the event loop (up to the
    100 MiB upload cap)."""
    return hashlib.sha256(data).hexdigest()


def _item_id(space_id: str | None, uploader_user_id: str) -> str:
    """A new upload's id: owner-bound (v_36) when it federates with a space
    album — no other household can announce it first as theirs."""
    if space_id is None:
        return uuid.uuid4().hex
    return mint_owner_bound_id(
        GALLERY_ITEM_KIND, space_id=space_id, owner_user_id=uploader_user_id
    )
