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
