"""Task lists exporter — a space's task lists (v_40).

Ships before ``tasks`` in :data:`RESOURCE_ORDER` so a joiner holds every
list before the tasks filed under it arrive. Records use the shared wire
form (:func:`socialhome.domain.task.task_list_to_wire_dict`), the same
shape ``SPACE_TASK_LIST_CREATED`` carries.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, TYPE_CHECKING

from .....domain.task import task_list_to_wire_dict

if TYPE_CHECKING:
    from .....repositories.task_repo import AbstractSpaceTaskRepo


class TaskListsExporter:
    resource = "task_lists"

    __slots__ = ("_repo",)

    def __init__(self, space_task_repo: "AbstractSpaceTaskRepo") -> None:
        self._repo = space_task_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        return await self._records(space_id, None)

    async def iter_changed(
        self, space_id: str, since: int
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """The lists created or renamed after ``since`` (§25.6 incremental,
        migration 0088)."""
        yield await self._records(space_id, since)

    async def _records(self, space_id: str, since: int | None) -> list[dict[str, Any]]:
        lists = await self._repo.list_lists(space_id, since_seq=since)
        return [task_list_to_wire_dict(lst, space_id) for lst in lists]
