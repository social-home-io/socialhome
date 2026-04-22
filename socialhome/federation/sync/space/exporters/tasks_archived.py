"""Archived tasks exporter.

v1 scope: the local repo has no "archived" flag — :class:`Task` carries
only ``status`` (todo/in_progress/done). Treat DONE tasks as the
archived set for sync purposes. A future enhancement may introduce a
dedicated archive flag.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from .....domain.task import TaskStatus

from .tasks import _task_to_dict

if TYPE_CHECKING:
    from .....repositories.task_repo import AbstractSpaceTaskRepo


class TasksArchivedExporter:
    resource = "tasks_archived"

    __slots__ = ("_repo",)

    def __init__(self, space_task_repo: "AbstractSpaceTaskRepo") -> None:
        self._repo = space_task_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        tasks = await self._repo.list_by_space(space_id)
        return [_task_to_dict(t) for t in tasks if t.status is TaskStatus.DONE]
