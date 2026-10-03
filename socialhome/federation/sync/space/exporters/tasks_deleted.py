"""Deleted tasks exporter — a space's single-task tombstones.

A task delete keeps its row as a tombstone (migration 0071). Streaming the
tombstones is how a household that missed ``SPACE_TASK_DELETED`` (offline
past the outbox, or the event dropped) learns of the delete: the receiver
tombstones its copy. A separate resource rather than a flag on ``tasks``
records, so an older receiver drops it as unknown instead of reading a
tombstone as a live task. Ships after ``task_lists_deleted`` and before
``tasks`` in :data:`RESOURCE_ORDER`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .....domain.task import task_tombstone_to_wire_dict

if TYPE_CHECKING:
    from .....repositories.task_repo import AbstractSpaceTaskRepo


#: Newest deletes first; a space that has deleted more tasks than this
#: streams the most recent ones. Trade-off: a household that missed more
#: than this many deletes in one outage keeps the older tasks — bounded
#: chunks over an unrealistic workload. Tombstones are never pruned.
MAX_TOMBSTONES_STREAMED: int = 500


class TasksDeletedExporter:
    resource = "tasks_deleted"

    __slots__ = ("_repo",)

    def __init__(self, space_task_repo: "AbstractSpaceTaskRepo") -> None:
        self._repo = space_task_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        tombstones = await self._repo.list_task_tombstones(
            space_id, limit=MAX_TOMBSTONES_STREAMED
        )
        return [task_tombstone_to_wire_dict(t, space_id) for t in tombstones]
