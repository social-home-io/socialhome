"""Deleted task lists exporter — a space's task-list tombstones.

A list delete keeps its row as a tombstone (migration 0069). Streaming the
tombstones is how a household that missed ``SPACE_TASK_LIST_DELETED``
(offline past the outbox, or the event dropped) learns of the delete: the
receiver tombstones its copy, and its tasks go with it. A separate
resource rather than a flag on ``task_lists`` records, so an older
receiver drops it as unknown instead of reading a tombstone as a live
list. Ships right after ``task_lists`` in :data:`RESOURCE_ORDER`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from .....domain.task import task_list_tombstone_to_wire_dict
from ..exporter import PagedExporterMixin
from ..window import SYNC_PAGE_SIZE, iter_tombstone_pages

if TYPE_CHECKING:
    from .....repositories.task_repo import AbstractSpaceTaskRepo


class TaskListsDeletedExporter(PagedExporterMixin):
    resource = "task_lists_deleted"

    __slots__ = ("_repo",)

    def __init__(self, space_task_repo: "AbstractSpaceTaskRepo") -> None:
        self._repo = space_task_repo

    async def iter_batches(self, space_id: str) -> AsyncIterator[list[dict[str, Any]]]:
        # Every tombstone, newest delete first, page by page: lists are not
        # governed by the space's retention, so no window applies and no
        # fixed count cuts the stream (a household that missed more
        # deletes than any cap would keep the rest forever).
        async def fetch(before: tuple[str, str] | None) -> list:
            return await self._repo.list_list_tombstones(
                space_id, limit=SYNC_PAGE_SIZE, before=before
            )

        async for page in iter_tombstone_pages(fetch, lambda t: (t.deleted_at, t.id)):
            yield [task_list_tombstone_to_wire_dict(t, space_id) for t in page]
