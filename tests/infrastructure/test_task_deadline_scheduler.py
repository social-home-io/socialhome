"""TaskDeadlineScheduler — due-today detection + idempotent fire."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from socialhome.domain.events import TaskDeadlineDue
from socialhome.domain.task import Task, TaskStatus
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.task_deadline_scheduler import (
    TaskDeadlineScheduler,
)


class _FakeTaskRepo:
    def __init__(self, tasks: list[Task]) -> None:
        self._tasks = tasks

    async def list_due_on(self, due: date) -> list[Task]:
        return [t for t in self._tasks if t.due_date == due]


class _FakeDb:
    """Minimal stand-in for AsyncDatabase — just the two calls the
    scheduler makes."""

    def __init__(self) -> None:
        self._rows: set[tuple[str, str]] = set()

    async def fetchone(self, _sql: str, params):
        task_id, due = params
        return {"1": 1} if (task_id, due) in self._rows else None

    async def enqueue(self, _sql: str, params):
        task_id, due = params
        self._rows.add((task_id, due))


def _task(tid: str, status: TaskStatus, due: date | None) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=tid,
        list_id="L",
        title=f"t{tid}",
        status=status,
        position=0,
        created_by="u",
        created_at=now,
        updated_at=now,
        due_date=due,
    )


@pytest.fixture
def env():
    today = date(2026, 4, 20)
    tasks = [
        _task("t1", TaskStatus.TODO, today),
        _task("t2", TaskStatus.DONE, today),  # skipped — already done
        _task("t3", TaskStatus.IN_PROGRESS, today),
        _task("t4", TaskStatus.TODO, date(2030, 1, 1)),  # not today
    ]
    bus = EventBus()
    fired: list[TaskDeadlineDue] = []
    bus.subscribe(TaskDeadlineDue, lambda e: fired.append(e))
    sched = TaskDeadlineScheduler(
        repo=_FakeTaskRepo(tasks),
        db=_FakeDb(),
        bus=bus,
    )
    return sched, fired, today


async def test_tick_fires_for_due_open_tasks(env):
    sched, fired, today = env
    n = await sched.tick_once(today=today)
    assert n == 2  # t1 + t3
    ids = sorted(e.task.id for e in fired)
    assert ids == ["t1", "t3"]


async def test_tick_is_idempotent(env):
    sched, fired, today = env
    await sched.tick_once(today=today)
    await sched.tick_once(today=today)
    # Still exactly 2 — second pass notices the dedupe rows.
    assert len(fired) == 2
