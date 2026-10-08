"""Tests for socialhome.federation.sync.space.exporters.tasks_deleted."""

from __future__ import annotations

from socialhome.domain.task import TaskTombstone
from socialhome.federation.sync.space.exporter import (
    REMOVAL_RESOURCES,
    RESOURCE_ORDER,
    ROSTER_RESOURCES,
)
from socialhome.federation.sync.space.exporters import TasksDeletedExporter
from socialhome.federation.sync.space.window import SYNC_PAGE_SIZE


class _Repo:
    def __init__(self) -> None:
        self.asked: list[tuple] = []

    async def list_task_tombstones(
        self, space_id, *, since=None, limit=500, before=None
    ):
        self.asked.append((space_id, since, limit, before))
        return [
            TaskTombstone(
                id="t1",
                list_id="l1",
                deleted_at="2026-06-01 10:00:00",
                created_by="u-c",
                deleted_by="u-d",
            )
        ]


async def test_exports_each_tombstone_as_the_delete_payload():
    repo = _Repo()
    exporter = TasksDeletedExporter(repo)
    assert exporter.resource == "tasks_deleted"
    assert await exporter.list_records("sp-1") == [
        {
            "id": "t1",
            "space_id": "sp-1",
            "list_id": "l1",
            "created_by": "u-c",
            "actor_user_id": "u-d",
        }
    ]
    # Every tombstone, page by page — no fixed count cuts the stream.
    assert repo.asked == [("sp-1", None, SYNC_PAGE_SIZE, None)]


def test_tombstones_stream_after_the_lists_and_before_the_tasks():
    order = list(RESOURCE_ORDER)
    assert order.index("task_lists_deleted") < order.index("tasks_deleted")
    assert order.index("tasks_deleted") < order.index("tasks")
    assert order.index("tasks_deleted") < order.index("tasks_archived")


def test_tombstones_are_a_removal_not_roster_or_content():
    assert "tasks_deleted" in REMOVAL_RESOURCES
    assert "tasks_deleted" not in ROSTER_RESOURCES
