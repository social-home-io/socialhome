"""Pages exporter — ``space_pages`` rows (v_48: with the host's ``seq``,
the version hash and the open conflict list, so a member household mirrors
the host's version by ``seq``). A household's own unacknowledged draft
never leaves it: such a page is exported as the canonical version the draft
was made from, or not at all (an unsequenced create)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from .....domain.page_version import page_record

if TYPE_CHECKING:
    from .....repositories.page_repo import AbstractPageRepo


class PagesExporter:
    resource = "pages"

    __slots__ = ("_repo",)

    def __init__(self, page_repo: AbstractPageRepo) -> None:
        self._repo = page_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        return await self._records(space_id, None)

    async def iter_changed(
        self, space_id: str, since: int
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """The pages changed after ``since`` (§25.6 incremental, migration
        0088): the row itself, or its draft base / conflict sides
        (``space_page_snapshots`` touches the page)."""
        yield await self._records(space_id, since)

    async def _records(self, space_id: str, since: int | None) -> list[dict[str, Any]]:
        pages = await self._repo.list(space_id=space_id, since_seq=since)
        out: list[dict[str, Any]] = []
        for p in pages:
            if p.pending_base_seq is not None:
                # An unacknowledged local draft never leaves the household:
                # export the canonical version it was made from instead.
                base = await self._repo.get_draft_base(p.id, space_id=space_id)
                if base is None or not base.title:
                    continue  # our own create the host has not sequenced
                p = replace(
                    p,
                    title=base.title,
                    content=base.content,
                    cover_image_url=base.cover_image_url,
                    seq=base.seq,
                )
            sides = await self._repo.list_conflict_sides(p.id, space_id=space_id)
            out.append(page_record(p, sides))
        return out
