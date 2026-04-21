"""Gallery domain types (§23.119, §4.2 Tier 1/2 sync rules).

Albums hold photos and videos. An album lives either at the household
level (``space_id is None``) or inside a specific space — that scoping
controls who can view, upload, and delete.

Per §25.6.2 S-9: only ``thumbnail_filename`` is sent on Tier-1 sync;
the full ``filename`` is fetched lazily via the ``gallery_item_full``
on-demand resource. The :func:`GalleryItem.to_thumbnail_dict` helper
captures the thumbnail-only projection for that path.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class GalleryAlbum:
    """One album of photos/videos."""

    id: str
    space_id: str | None  # None = household-level
    owner_user_id: str
    name: str
    description: str | None = None
    cover_item_id: str | None = None
    item_count: int = 0
    cover_url: str | None = None  # convenience — not persisted
    retention_exempt: bool = False
    created_at: str | None = None
    updated_at: str | None = None


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
    created_at: str | None = None

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
