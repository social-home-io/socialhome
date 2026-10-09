"""Deleted gallery albums and items exporters — a space's gallery
tombstones (§25.6, migration 0085). See :mod:`.row_tombstones` for the
shared shape.

Two resources around ``gallery`` in :data:`RESOURCE_ORDER`: album
tombstones before it (an album delete takes its items; the receiver's 0085
trigger tombstones them there too), single-item tombstones after it — each
names its album, which a host stub for an item never held needs held here,
and a joiner gets the albums from ``gallery`` (the comment tombstones
follow the posts for the same reason). The host's live stream never holds
a deleted item, so nothing streams it back in between. Items of a deleted
album are not streamed as item tombstones: the album's covers them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .row_tombstones import RowTombstonesExporter

if TYPE_CHECKING:
    from .....repositories.gallery_repo import AbstractGalleryRepo


class GalleryAlbumsDeletedExporter(RowTombstonesExporter):
    resource = "gallery_albums_deleted"
    owner_key = "owner_user_id"

    __slots__ = ()

    def __init__(self, gallery_repo: "AbstractGalleryRepo") -> None:
        super().__init__(gallery_repo.list_album_tombstones_page)


class GalleryItemsDeletedExporter(RowTombstonesExporter):
    resource = "gallery_items_deleted"
    owner_key = "uploaded_by"
    parent_key = "album_id"

    __slots__ = ()

    def __init__(self, gallery_repo: "AbstractGalleryRepo") -> None:
        super().__init__(gallery_repo.list_item_tombstones_page)
