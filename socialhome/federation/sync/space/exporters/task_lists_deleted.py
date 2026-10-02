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

from typing import TYPE_CHECKING, Any

from .....domain.task import task_list_tombstone_to_wire_dict

if TYPE_CHECKING:
    from .....repositories.task_repo import AbstractSpaceTaskRepo


#: Newest deletes first; a space that has deleted more lists than this
#: streams the most recent ones. Trade-off: a household that missed more
#: than this many deletes in one outage keeps the older lists — bounded
#: chunks over an unrealistic workload. Tombstones are never pruned.
MAX_TOMBSTONES_STREAMED: int = 500


class TaskListsDeletedExporter:
    resource = "task_lists_deleted"

    __slots__ = ("_repo",)

    def __init__(self, space_task_repo: "AbstractSpaceTaskRepo") -> None:
        self._repo = space_task_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        tombstones = await self._repo.list_list_tombstones(
            space_id, limit=MAX_TOMBSTONES_STREAMED
        )
        return [task_list_tombstone_to_wire_dict(t, space_id) for t in tombstones]
