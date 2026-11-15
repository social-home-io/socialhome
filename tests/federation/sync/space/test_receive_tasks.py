"""``receive_tasks``: task lists, tasks and their tombstones by §25.6 sync."""

from __future__ import annotations

from datetime import UTC, date, datetime

from socialhome.domain.task import Task, TaskPriority, TaskStatus
from tests.federation.sync.space.receiver_fakes import _send


async def test_tasks(setup):
    r, c, kp = setup
    await _send(
        r,
        kp,
        "tasks",
        [
            {
                "id": "t-1",
                "list_id": "list-1",
                "title": "X",
                "status": "todo",
                "created_by": "u-1",
            },
        ],
    )
    assert len(c.tasks) == 1
    _, task = c.tasks[0]
    assert task.id == "t-1"


async def test_a_task_deleted_here_is_skipped(setup):
    """Migration 0071: a tombstoned task id never comes back via the
    ``tasks`` / ``tasks_archived`` streams of a provider that missed it."""
    r, c, kp = setup
    c.deleted_task_ids.add("t-gone")
    for resource in ("tasks", "tasks_archived"):
        await _send(
            r,
            kp,
            resource,
            [
                {
                    "id": "t-gone",
                    "list_id": "list-1",
                    "title": "X",
                    "status": "todo",
                    "created_by": "u-1",
                },
            ],
        )
    assert c.tasks == []


async def test_task_lists(setup):
    """v_40: the ``task_lists`` resource files each list under the space;
    a record without an id, name or creator is skipped."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "task_lists",
        [
            {"id": "l-1", "name": "Chores", "created_by": "u-1"},
            {"id": "l-2", "name": "", "created_by": "u-1"},
            {"id": "l-3", "name": "No creator"},
        ],
    )
    assert [(sp, lst.id, lst.name) for sp, lst in c.task_lists] == [
        ("sp-1", "l-1", "Chores")
    ]


def _held_task(**kw):
    at = datetime(2026, 9, 1, tzinfo=UTC)
    base = {
        "id": "t-held",
        "list_id": "list-1",
        "title": "Held",
        "status": TaskStatus.TODO,
        "position": 0,
        "created_by": "u-1",
        "created_at": at,
        "updated_at": at,
        "due_date": date(2026, 10, 3),
        "archived_at": at,
        "priority": TaskPriority.HIGH,
        "labels": ("Bills",),
    }
    base.update(kw)
    return Task(**base)


async def test_v39_host_sync_does_not_wipe_held_fields(setup):
    """I1: a v39 host's chunk (no ``priority`` key, the due date / archive
    its inbound lost sent as null) used to be parsed as a NEW row and
    upserted over ours every tick — wiping priority, labels, due date and
    archive. It now merges onto the held row."""
    r, c, kp = setup
    c.held_tasks["t-held"] = ("sp-1", _held_task())
    await _send(
        r,
        kp,
        "tasks",
        [
            {
                "id": "t-held",
                "list_id": "list-1",
                "title": "Renamed on v39",
                "status": "in_progress",
                "created_by": "u-1",
                "due_date": None,
                "archived_at": None,
            }
        ],
    )
    _, task = c.tasks[-1]
    assert task.title == "Renamed on v39"
    assert task.status is TaskStatus.IN_PROGRESS
    assert task.priority is TaskPriority.HIGH
    assert task.labels == ("Bills",)
    assert task.due_date == date(2026, 10, 3)
    assert task.archived_at == datetime(2026, 9, 1, tzinfo=UTC)


async def test_v40_host_sync_may_clear_held_fields(setup):
    r, c, kp = setup
    c.held_tasks["t-held"] = ("sp-1", _held_task())
    await _send(
        r,
        kp,
        "tasks",
        [
            {
                "id": "t-held",
                "list_id": "list-1",
                "title": "Held",
                "created_by": "u-1",
                "due_date": None,
                "archived_at": None,
                "priority": None,
                "labels": [],
            }
        ],
    )
    _, task = c.tasks[-1]
    assert task.priority is None and task.labels == ()
    assert task.due_date is None and task.archived_at is None


async def test_sync_never_merges_onto_another_spaces_row(setup):
    """The held row of another space is not ``existing`` — the scoped repo
    refuses the write itself."""
    r, c, kp = setup
    c.held_tasks["t-held"] = ("sp-other", _held_task())
    await _send(
        r,
        kp,
        "tasks",
        [{"id": "t-held", "list_id": "list-1", "title": "x", "created_by": "u-1"}],
    )
    _, task = c.tasks[-1]
    assert task.priority is None and task.labels == ()


async def test_tasks_carry_due_date_archived_priority_labels(setup):
    """The receiver used to drop ``due_date`` and ``archived_at``; it now
    reads the shared wire codec."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "tasks_archived",
        [
            {
                "id": "t-2",
                "list_id": "list-1",
                "title": "X",
                "status": "done",
                "created_by": "u-1",
                "due_date": "2026-10-03",
                "archived_at": "2026-09-01T00:00:00+00:00",
                "priority": "urgent",
                "labels": ["Bills", "bills"],
            },
        ],
    )
    _, task = c.tasks[0]
    assert task.due_date == date(2026, 10, 3)
    assert task.archived_at == datetime(2026, 9, 1, tzinfo=UTC)
    assert task.priority is TaskPriority.URGENT
    assert task.labels == ("Bills",)


async def test_tasks_archived_routes_to_task_repo(setup):
    r, c, kp = setup
    await _send(
        r,
        kp,
        "tasks_archived",
        [
            {
                "id": "t-done",
                "list_id": "list-1",
                "title": "done one",
                "status": "done",
                "created_by": "u-1",
            },
        ],
    )
    assert len(c.tasks) == 1
