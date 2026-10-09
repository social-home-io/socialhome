"""Gallery exporter — albums + items for a space.

Albums + items stream together under the ``gallery`` resource. Every
album ships first (the receiver files an item only into an album it
holds), then every item of the space, page by page — no retention window:
nothing prunes gallery items (the retention sweep touches posts and chat
only), so windowing them would silently hide from a joiner photos the
host still shows. A mirror of a post's media (``source_post_id``) never
ships: the source post federates on its own and the receiver's
``SystemAlbumBridge`` re-creates the mirror locally.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any, TYPE_CHECKING

from ..exporter import PagedExporterMixin
from ..window import SYNC_PAGE_SIZE, iter_pages

if TYPE_CHECKING:
    from .....domain.gallery import GalleryAlbum, GalleryItem
    from .....repositories.gallery_repo import AbstractGalleryRepo


class GalleryExporter(PagedExporterMixin):
    resource = "gallery"

    __slots__ = ("_repo",)

    def __init__(self, gallery_repo: "AbstractGalleryRepo") -> None:
        self._repo = gallery_repo

    def iter_batches(self, space_id: str) -> AsyncIterator[list[dict[str, Any]]]:
        return self._pages(space_id, None)

    def iter_changed(
        self, space_id: str, since: int
    ) -> AsyncIterator[list[dict[str, Any]]]:
        return self._pages(space_id, since)

    async def _pages(
        self, space_id: str, since: int | None
    ) -> AsyncIterator[list[dict[str, Any]]]:
        # The system album rides along so the receiver creates it
        # pre-populated; without it, mirrored items from forthcoming posts
        # would have nowhere to live until the first local feed event.
        async def albums(cursor: int | None) -> tuple[list["GalleryAlbum"], int | None]:
            return await self._repo.list_albums_sync_page(
                space_id, cursor=cursor, limit=SYNC_PAGE_SIZE, since=since
            )

        async for album_page in iter_pages(albums):
            yield [{"kind": "album", **asdict(a)} for a in album_page]

        async def items(cursor: int | None) -> tuple[list["GalleryItem"], int | None]:
            return await self._repo.list_items_sync_page(
                space_id, cursor=cursor, limit=SYNC_PAGE_SIZE, since=since
            )

        async for item_page in iter_pages(items):
            yield [{"kind": "item", **asdict(it)} for it in item_page]
