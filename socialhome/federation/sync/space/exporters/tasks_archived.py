"""Archived tasks exporter.

Surfaces tasks the user has explicitly archived (``archived_at`` set)
for the §4.2.3 initial-sync resource catalog.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, TYPE_CHECKING

from .....domain.task import task_to_wire_dict

if TYPE_CHECKING:
    from .....repositories.task_repo import AbstractSpaceTaskRepo


class TasksArchivedExporter:
    resource = "tasks_archived"

    __slots__ = ("_repo",)

    def __init__(self, space_task_repo: "AbstractSpaceTaskRepo") -> None:
        self._repo = space_task_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        return await self._records(space_id, None)

    async def iter_changed(
        self, space_id: str, since: int
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """The archived tasks changed after ``since`` (§25.6 incremental,
        migration 0088) — archiving stamps the task."""
        yield await self._records(space_id, since)

    async def _records(self, space_id: str, since: int | None) -> list[dict[str, Any]]:
        tasks = await self._repo.list_by_space(space_id, since_seq=since)
        return [
            task_to_wire_dict(t, space_id) for t in tasks if t.archived_at is not None
        ]
