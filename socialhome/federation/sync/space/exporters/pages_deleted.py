"""Deleted pages exporter — a space's page tombstones.

A space page delete keeps its row as a tombstone (migration 0073).
Streaming the tombstones is how a household that missed
``SPACE_PAGE_DELETED`` (offline past the outbox, or the envelope lost)
learns of the delete: the receiver tombstones its copy, and — from the
host — records a stub for an id it never held, so no stale copy streamed
later can create it. A separate resource rather than a flag on ``pages``
records, so an older receiver drops it as unknown instead of reading a
tombstone as a live page. Ships before ``pages`` in :data:`RESOURCE_ORDER`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from .....domain.page import page_tombstone_to_wire_dict
from ..exporter import PagedExporterMixin
from ..window import SYNC_PAGE_SIZE, iter_tombstone_pages

if TYPE_CHECKING:
    from .....repositories.page_repo import AbstractPageRepo


class PagesDeletedExporter(PagedExporterMixin):
    resource = "pages_deleted"

    __slots__ = ("_repo",)

    def __init__(self, page_repo: "AbstractPageRepo") -> None:
        self._repo = page_repo

    def iter_batches(self, space_id: str) -> AsyncIterator[list[dict[str, Any]]]:
        return self._pages(space_id, None)

    def iter_changed(
        self, space_id: str, since: int
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """The tombstones written after ``since`` (§25.6 incremental,
        migration 0088) — same keyset, filtered by the stamp."""
        return self._pages(space_id, since)

    async def _pages(
        self, space_id: str, since: int | None
    ) -> AsyncIterator[list[dict[str, Any]]]:
        # Every tombstone, newest delete first, page by page: pages are not
        # governed by the space's retention, so no window applies and no
        # fixed count cuts the stream (a household that missed more
        # deletes than any cap would keep the rest forever).
        async def fetch(before: tuple[str, str] | None) -> list:
            return await self._repo.list_page_tombstones(
                space_id, limit=SYNC_PAGE_SIZE, before=before, since_seq=since
            )

        async for page in iter_tombstone_pages(fetch, lambda t: (t.deleted_at, t.id)):
            yield [page_tombstone_to_wire_dict(t, space_id) for t in page]
