"""Tests for socialhome.federation.sync.space.exporters.task_lists_deleted."""

from __future__ import annotations

from socialhome.domain.task import TaskListTombstone
from socialhome.federation.sync.space.exporter import (
    REMOVAL_RESOURCES,
    RESOURCE_ORDER,
    ROSTER_RESOURCES,
)
from socialhome.federation.sync.space.exporters import TaskListsDeletedExporter
from socialhome.federation.sync.space.exporters.task_lists_deleted import (
    MAX_TOMBSTONES_STREAMED,
)


class _Repo:
    def __init__(self) -> None:
        self.asked: list[tuple] = []

    async def list_list_tombstones(self, space_id, *, since=None, limit=500):
        self.asked.append((space_id, since, limit))
        return [
            TaskListTombstone(
                id="l1",
                deleted_at="2026-06-01 10:00:00",
                created_by="u-c",
                deleted_by="u-d",
            )
        ]


async def test_exports_each_tombstone_as_the_delete_payload():
    repo = _Repo()
    exporter = TaskListsDeletedExporter(repo)
    assert exporter.resource == "task_lists_deleted"
    assert await exporter.list_records("sp-1") == [
        {"id": "l1", "space_id": "sp-1", "created_by": "u-c", "actor_user_id": "u-d"}
    ]
    assert repo.asked == [("sp-1", None, MAX_TOMBSTONES_STREAMED)]


def test_tombstones_stream_after_the_lists_and_before_the_tasks():
    order = list(RESOURCE_ORDER)
    assert order.index("task_lists") < order.index("task_lists_deleted")
    assert order.index("task_lists_deleted") < order.index("tasks")


def test_tombstones_are_a_removal_not_roster_or_content():
    assert "task_lists_deleted" in REMOVAL_RESOURCES
    assert "task_lists_deleted" not in ROSTER_RESOURCES
