"""Tasks exporter — active space tasks (not archived)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .....repositories.task_repo import AbstractSpaceTaskRepo


class TasksExporter:
    resource = "tasks"

    __slots__ = ("_repo",)

    def __init__(self, space_task_repo: "AbstractSpaceTaskRepo") -> None:
        self._repo = space_task_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        tasks = await self._repo.list_by_space(space_id)
        return [_task_to_dict(t) for t in tasks]


def _task_to_dict(task) -> dict[str, Any]:
    d = asdict(task)
    for field in ("created_at", "updated_at"):
        v = d.get(field)
        if v is not None and not isinstance(v, str):
            d[field] = v.isoformat()
    if d.get("due_date") is not None and not isinstance(d["due_date"], str):
        d["due_date"] = d["due_date"].isoformat()
    if d.get("status") and not isinstance(d["status"], str):
        d["status"] = d["status"].value
    # assignees is a tuple — JSON wants a list.
    d["assignees"] = list(d.get("assignees") or ())
    # Recurrence rule is a nested dataclass.
    rec = d.get("recurrence")
    if rec is not None and not isinstance(rec, dict):
        d["recurrence"] = asdict(rec)
    return d
