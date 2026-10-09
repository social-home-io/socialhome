"""Tests for socialhome.federation.sync.space.exporters.task_lists."""

from __future__ import annotations

from socialhome.domain.task import TaskList
from socialhome.federation.sync.space.exporter import RESOURCE_ORDER
from socialhome.federation.sync.space.exporters import TaskListsExporter


class _Repo:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def list_lists(self, space_id, *, since_seq=None):
        self.asked.append(space_id)
        return [TaskList(id="l1", name="Chores", created_by="u1")]


async def test_task_lists_exporter_uses_the_wire_codec():
    repo = _Repo()
    exporter = TaskListsExporter(repo)
    assert exporter.resource == "task_lists"
    recs = await exporter.list_records("sp-1")
    assert repo.asked == ["sp-1"]
    assert recs == [
        {"id": "l1", "space_id": "sp-1", "name": "Chores", "created_by": "u1"}
    ]


def test_task_lists_stream_before_tasks():
    """A space task is only stored under a list the receiver holds."""
    order = list(RESOURCE_ORDER)
    assert order.index("task_lists") < order.index("tasks")
    assert order.index("task_lists") < order.index("tasks_archived")
