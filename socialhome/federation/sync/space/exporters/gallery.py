"""Gallery exporter — albums + items for a space.

Albums + items stream together under the ``gallery`` resource. The
receiver restores albums first (parent row) then items, using the
``album_id`` foreign key for correct ordering.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .....repositories.gallery_repo import AbstractGalleryRepo


class GalleryExporter:
    resource = "gallery"

    __slots__ = ("_repo",)

    def __init__(self, gallery_repo: "AbstractGalleryRepo") -> None:
        self._repo = gallery_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        albums = await self._repo.list_albums(space_id, limit=1000)
        out: list[dict[str, Any]] = []
        # Emit albums first so they land before their items.
        for a in albums:
            out.append({"kind": "album", **asdict(a)})
        for a in albums:
            items = await self._repo.list_items(a.id, limit=1000)
            for it in items:
                out.append({"kind": "item", **asdict(it)})
        return out
