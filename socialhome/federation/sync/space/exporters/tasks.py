"""Tasks exporter — active space tasks (not archived).

Records use the shared task wire form
(:func:`socialhome.domain.task.task_to_wire_dict`), the same shape the
live ``SPACE_TASK_*`` events carry.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, TYPE_CHECKING

from .....domain.task import task_to_wire_dict

if TYPE_CHECKING:
    from .....repositories.task_repo import AbstractSpaceTaskRepo


class TasksExporter:
    resource = "tasks"

    __slots__ = ("_repo",)

    def __init__(self, space_task_repo: "AbstractSpaceTaskRepo") -> None:
        self._repo = space_task_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        return await self._records(space_id, None)

    async def iter_changed(
        self, space_id: str, since: int
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """The active tasks changed after ``since`` (§25.6 incremental,
        migration 0088) — an unarchived task included: unarchiving stamps it."""
        yield await self._records(space_id, since)

    async def _records(self, space_id: str, since: int | None) -> list[dict[str, Any]]:
        tasks = await self._repo.list_by_space(space_id, since_seq=since)
        return [task_to_wire_dict(t, space_id) for t in tasks if t.archived_at is None]
