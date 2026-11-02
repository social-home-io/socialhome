"""Gallery domain types (§23.119, §4.2 Tier 1/2 sync rules).

Albums hold photos and videos. An album lives either at the household
level (``space_id is None``) or inside a specific space — that scoping
controls who can view, upload, and delete.

§25.6.2 S-9 describes a thumbnail-only Tier-1 projection with the full
file fetched lazily via a ``gallery_item_full`` on-demand resource. That
resource was never built: the sender pushes the full file to every member
household over the media outbox instead, so the federated item carries
its full ``url`` too (:func:`GalleryItem.to_federation_dict`) — the
receiver's row must name the file its ``SPACE_MEDIA_BLOB`` delivers.
:func:`GalleryItem.to_thumbnail_dict` remains the thumbnail-only
projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


@dataclass(slots=True, frozen=True)
class GalleryAlbum:
    """One album of photos/videos."""

    id: str
    space_id: str | None  # None = household-level
    # NULL for the auto-mirrored ``is_system`` "Posts" album, which has
    # no human owner; otherwise the creator's user_id.
    owner_user_id: str | None
    name: str
    description: str | None = None
    cover_item_id: str | None = None
    item_count: int = 0
    cover_url: str | None = None  # convenience — not persisted
    retention_exempt: bool = False
    # ``True`` for the auto-mirrored "Posts" album that surfaces every
    # photo/video shared via a feed post. The album cannot be deleted,
    # renamed, or directly uploaded to — items appear and disappear
    # strictly with their source post.
    is_system: bool = False
    created_at: str | None = None
    updated_at: str | None = None
    # The newest live item's ``created_at``, else the album's own — the
    # albums list's "newest activity first" order and its cursor. Read
    # with the list, not persisted.
    activity_at: str | None = None
    # The household's default album (``instance_config``
    # ``gallery.default_album_id``) — not persisted on the row.
    is_default: bool = False
    # ``True`` while nobody renamed the default album: each viewer then
    # sees it under their own word for "Photos" (the SPA localises it),
    # not the first uploader's. Not persisted on the row.
    default_name: bool = False

    def to_federation_dict(self) -> dict:
        """The album as ``SPACE_GALLERY_ALBUM_CREATED`` / ``_UPDATED`` carry it.

        Only what a member household renders. ``item_count`` stays out (the
        receiver counts the items it actually holds), and so do
        ``is_system`` (the system album never federates) and
        ``retention_exempt`` (a per-household purge setting).
        """
        return {
            "id": self.id,
            "owner_user_id": self.owner_user_id,
            "name": self.name,
            "description": self.description,
            "cover_item_id": self.cover_item_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True, frozen=True)
class GalleryItem:
    """A single photo or video in an album."""

    id: str
    album_id: str
    uploaded_by: str
    item_type: str  # 'photo' | 'video'
    url: str  # /api/media/{filename}
    thumbnail_url: str  # /api/media/{thumbnail_filename}
    width: int
    height: int
    duration_s: float | None = None  # None for photos
    caption: str | None = None
    taken_at: str | None = None  # ISO 8601 day-precision (YYYY-MM-DD)
    sort_order: int = 0
    # Set when the item was mirrored from a feed post (system album);
    # ``None`` for direct user uploads. Drives bulk cleanup on
    # post-delete and lets the UI deep-link a thumbnail back to its
    # source post.
    source_post_id: str | None = None
    created_at: str | None = None

    @property
    def day(self) -> str:
        """The day the album sorts the item by: the day taken, else the
        upload day (UTC) — the ``idx_gallery_items_album_taken``
        expression (migration 0139)."""
        return self.taken_at or (self.created_at or "")[:10]

    def to_federation_dict(self) -> dict:
        """The item as ``SPACE_GALLERY_ITEM_CREATED`` carries it to a member.

        The thumbnail projection **plus** the full ``url``. The sender pushes
        both files to every member household over the media outbox anyway
        (no on-demand fetch path exists), and a receiver's row must name the
        full file: the gallery opens it on zoom, and the ``SPACE_MEDIA_BLOB``
        scope check accepts only the files the row references — without the
        ``url`` it refused the full-size picture of every federated item.
        The §25.6 sync exporter has always shipped it (``asdict``).
        """
        return {**self.to_thumbnail_dict(), "url": self.url}

    def to_thumbnail_dict(self) -> dict:
        """S-9: thumbnail-only projection used in Tier-1 sync.

        Excludes the full-resolution ``url`` so a remote instance can
        render the album grid without being able to direct-download
        every original file.
        """
        return {
            "id": self.id,
            "album_id": self.album_id,
            "uploaded_by": self.uploaded_by,
            "item_type": self.item_type,
            "thumbnail_url": self.thumbnail_url,
            "width": self.width,
            "height": self.height,
            "duration_s": self.duration_s,
            "caption": self.caption,
            "taken_at": self.taken_at,
            "sort_order": self.sort_order,
            "created_at": self.created_at,
        }


#: :class:`GalleryAlbum` fields that describe the album as one viewer of
#: this household sees it (read with the list, never stored). They never
#: leave the household: the §25.6 exporter drops them from its records.
ALBUM_VIEW_FIELDS: frozenset[str] = frozenset(
    {"activity_at", "is_default", "default_name"}
)


# ─── Library model (migration 0139) ──────────────────────────────────────

#: Days a deleted household photo or video stays in the trash before the
#: purge removes it for good (owner decision D5, 2026-11-02).
TRASH_RETENTION_DAYS: int = 30

#: ``gallery_albums.audience`` — everyone at home (the default) …
AUDIENCE_EVERYONE: str = "everyone"
#: … or only the owner and the people in ``gallery_album_audience``
#: (a private household album; a space album is always ``everyone``).
AUDIENCE_CHOSEN: str = "chosen"
AUDIENCES: frozenset[str] = frozenset({AUDIENCE_EVERYONE, AUDIENCE_CHOSEN})


class UploadOutcome(StrEnum):
    """What an upload did. A household upload whose bytes match a photo the
    uploader can already see stores nothing new (owner: exact duplicates
    are skipped and counted); one matching a photo in the trash brings that
    photo back instead."""

    CREATED = "created"
    ALREADY_HERE = "already_here"
    RESTORED = "restored"


@dataclass(slots=True, frozen=True)
class GalleryUpload:
    """The result of :meth:`GalleryService.upload_item`: the item the
    upload created, or the existing one it matched."""

    item: GalleryItem
    outcome: UploadOutcome = UploadOutcome.CREATED


@dataclass(slots=True, frozen=True)
class GalleryDuplicate:
    """A household item whose uploaded bytes hash to the same
    ``content_sha256`` — live, or in the trash (``in_trash``)."""

    item: GalleryItem
    in_trash: bool = False


@dataclass(slots=True, frozen=True)
class GalleryTrashItem:
    """A household photo or video in the trash: the item as it was, who
    deleted it and when (``deleted_at`` is naive UTC, SQLite
    ``datetime('now')``), and the album it returns to on restore — which
    may be in the trash itself (an album delete), then it comes back too."""

    item: GalleryItem
    deleted_at: str
    deleted_by: str | None
    album_name: str
    album_owner_user_id: str | None
    album_in_trash: bool = False


@dataclass(slots=True, frozen=True)
class GalleryAlbumAudience:
    """Who sees a household album: ``audience`` is :data:`AUDIENCE_EVERYONE`
    or :data:`AUDIENCE_CHOSEN`; ``user_ids`` are the chosen people (the
    owner always sees their album and is not listed)."""

    album_id: str
    audience: str = AUDIENCE_EVERYONE
    user_ids: frozenset[str] = frozenset()


# ─── Uploads ─────────────────────────────────────────────────────────────

#: Largest photo upload, and largest video upload to a *space* album (a
#: space keeps the protocol's 60 s, which fits). Enforced while
#: streaming, so it holds for chunked bodies too and is independent of
#: aiohttp's 1 MiB ``client_max_size``.
GALLERY_MAX_UPLOAD_BYTES: int = 100 * 1024 * 1024


def is_video_upload(content_type: str) -> bool:
    """Does an upload with this ``Content-Type`` go the video way?

    A type-less body (``application/octet-stream``) is treated as a video
    — the SPA always names a photo's type.
    """
    return (
        content_type.startswith("video/") or content_type == "application/octet-stream"
    )


@dataclass(slots=True, frozen=True)
class UploadLimits:
    """The largest files one album takes (:meth:`GalleryService.upload_limits`)."""

    photo_bytes: int
    video_bytes: int
    #: Longest video it keeps whole: the household gallery's own limit,
    #: or the protocol's for a space (whose videos are cut to it).
    video_seconds: int


@dataclass(slots=True, frozen=True)
class StashedUpload:
    """An uploaded file written to the (non-served) transcode temp dir
    while it streamed in — never held in memory."""

    path: str
    size: int
    #: SHA-256 (hex) of the bytes as uploaded — the duplicate check.
    sha256: str


# ─── Household videos (owner decision D4, 2026-11-02) ────────────────────
#
# A household gallery is not the federated feed: its videos never leave
# the household, so it keeps longer videos than the protocol's 60 s
# (``VIDEO_MAX_DURATION_SECONDS``, which spaces, the feed and DMs keep).
# The stored format is the same everywhere — VP9/Opus WebM, longest edge
# ``VIDEO_MAX_DIMENSION`` — only the length and the upload size differ.

#: Longest household-gallery video. Checked before upload (the SPA reads
#: the length from the file) and again on the server; a longer one is
#: refused with its length, never cut. "Up to 5 minutes" is what a phone
#: shows as ``5:00``, so anything under 301 s passes.
GALLERY_VIDEO_MAX_SECONDS: int = 300

#: Largest household-gallery video upload. Five minutes of phone video:
#: 1080p30 H.264 ≈ 650 MB, 4K30 HEVC ≈ 850 MB, 4K30 H.264 ≈ 1.75 GB,
#: 4K60 HEVC ≈ 2.0 GB — 2 GiB covers all of them (only 4K60 H.264,
#: ≈ 3 GB, is over and is refused before a byte is sent). The upload is
#: streamed to disk in chunks, never held in memory, and the stored WebM
#: is a fraction of it.
GALLERY_VIDEO_MAX_UPLOAD_BYTES: int = 2 * 1024 * 1024 * 1024

#: Videos waiting to be converted, household-wide. A further upload is
#: refused (503 ``TRANSCODE_QUEUE_FULL``) until the queue drains, so a
#: runaway client can't park unbounded source files on disk.
GALLERY_VIDEO_MAX_QUEUED: int = 300


def video_fits(duration_s: float, max_seconds: int) -> bool:
    """Does a video of ``duration_s`` keep within ``max_seconds``?

    Whole seconds, the way a phone shows a length: ``5:00.9`` is five
    minutes, ``5:01`` is not.
    """
    return int(duration_s) <= max_seconds


class ItemSort(StrEnum):
    """How an album lists its items. ``ADDED`` is the upload order
    (newest first — the order before migration 0139, kept for old
    clients); ``TAKEN`` / ``TAKEN_ASC`` sort by :attr:`GalleryItem.day`,
    newest or oldest first (owner decision D1: inside an album, by date
    taken)."""

    ADDED = "added"
    TAKEN = "taken"
    TAKEN_ASC = "taken_asc"


@dataclass(slots=True, frozen=True)
class ItemCursor:
    """Where the next page of a date-sorted album starts: the last item's
    :attr:`GalleryItem.day`, ``created_at`` and ``id`` — the full sort
    key, so no item is skipped or repeated between pages."""

    day: str
    created_at: str
    item_id: str


# ─── On this day (owner decision D14) ─────────────────────────────────────

#: Previews an "On this day" card carries.
ON_THIS_DAY_PREVIEWS: int = 3
#: Albums one person can hide from "On this day" — bounds the
#: preferences blob.
ON_THIS_DAY_MAX_EXCLUDED: int = 200


@dataclass(slots=True, frozen=True)
class OnThisDaySettings:
    """One person's "On this day" settings (``users.preferences_json``
    ``gallery_on_this_day``): on unless they turned it off, the day they
    hid the card with ✕ (it shows again the next day) and the albums they
    never want it to use."""

    enabled: bool = True
    hidden_on: str | None = None
    excluded_album_ids: frozenset[str] = frozenset()


@dataclass(slots=True, frozen=True)
class OnThisDayCard:
    """Photos taken on this calendar day in an earlier year, from one
    album: ``date`` (``YYYY-MM-DD``) is the day they were taken,
    ``count`` how many, ``items`` the first few (oldest first)."""

    album: GalleryAlbum
    date: str
    years_ago: int
    count: int
    items: tuple[GalleryItem, ...] = ()
