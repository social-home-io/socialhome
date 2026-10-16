"""TaskDeadlineScheduler — due-today detection + idempotent fire."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from socialhome.domain.events import TaskDeadlineDue
from socialhome.domain.task import Task, TaskStatus
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.task_deadline_scheduler import (
    DEADLINE_NOTIF_RETENTION_DAYS,
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

    async def enqueue(self, sql: str, params):
        if "DELETE" in sql.upper():
            (cutoff,) = params
            self._rows = {(t, d) for (t, d) in self._rows if d >= cutoff}
            return
        task_id, due = params
        self._rows.add((task_id, due))


def _task(tid: str, status: TaskStatus, due: date | None) -> Task:
    now = datetime.now(UTC)
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


async def test_tick_prunes_stale_dedupe_rows(env):
    """``tick_once`` drops dedupe rows older than the retention window."""
    sched, _fired, today = env
    db = sched._db
    old = (today - timedelta(days=DEADLINE_NOTIF_RETENTION_DAYS + 1)).isoformat()
    recent = (today - timedelta(days=1)).isoformat()
    db._rows.add(("old-task", old))
    db._rows.add(("recent-task", recent))

    await sched.tick_once(today=today)

    assert ("old-task", old) not in db._rows
    assert ("recent-task", recent) in db._rows


# ─── Household time zone ──────────────────────────────────────────────


class _FakePreferences:
    """Stand-in for :class:`PreferencesService` — only ``get_household``."""

    def __init__(self, tz: str) -> None:
        self._tz = tz

    async def get_household(self) -> SimpleNamespace:
        return SimpleNamespace(tz=self._tz)


class _BrokenPreferences:
    async def get_household(self) -> SimpleNamespace:
        raise RuntimeError("preferences table unreadable")


def _tz_env(preferences) -> tuple[TaskDeadlineScheduler, list[TaskDeadlineDue]]:
    tasks = [
        _task("apr20", TaskStatus.TODO, date(2026, 4, 20)),
        _task("apr21", TaskStatus.TODO, date(2026, 4, 21)),
    ]
    bus = EventBus()
    fired: list[TaskDeadlineDue] = []

    async def _on_due(event: TaskDeadlineDue) -> None:
        fired.append(event)

    bus.subscribe(TaskDeadlineDue, _on_due)
    sched = TaskDeadlineScheduler(
        repo=_FakeTaskRepo(tasks),
        db=_FakeDb(),
        bus=bus,
        preferences=preferences,
    )
    return sched, fired


async def test_tick_uses_household_date_west_of_utc():
    """Los Angeles, 20:00 on Apr 20 local = 03:00 UTC on Apr 21: only the
    task due on the household's Apr 20 fires — not tomorrow's."""
    sched, fired = _tz_env(_FakePreferences("America/Los_Angeles"))
    now = datetime(2026, 4, 21, 3, 0, tzinfo=UTC)

    assert await sched.tick_once(now=now) == 1

    assert [e.task.id for e in fired] == ["apr20"]
    assert fired[0].due_date == date(2026, 4, 20)
    # Dedupe row is keyed on the household date too.
    assert ("apr20", "2026-04-20") in sched._db._rows


async def test_tick_uses_household_date_east_of_utc():
    """Auckland, 01:00 on Apr 21 local = 13:00 UTC on Apr 20: today's
    (Apr 21) task fires now, not ~13 h later at UTC midnight."""
    sched, fired = _tz_env(_FakePreferences("Pacific/Auckland"))
    now = datetime(2026, 4, 20, 13, 0, tzinfo=UTC)

    assert await sched.tick_once(now=now) == 1

    assert [e.task.id for e in fired] == ["apr21"]
    assert fired[0].due_date == date(2026, 4, 21)


async def test_tick_falls_back_to_utc_when_preferences_fail(caplog):
    sched, fired = _tz_env(_BrokenPreferences())
    now = datetime(2026, 4, 21, 3, 0, tzinfo=UTC)

    with caplog.at_level(logging.WARNING):
        assert await sched.tick_once(now=now) == 1

    assert [e.task.id for e in fired] == ["apr21"]
    assert "household tz" in caplog.text


async def test_tick_without_preferences_uses_utc():
    sched, fired = _tz_env(None)
    now = datetime(2026, 4, 20, 13, 0, tzinfo=UTC)

    assert await sched.tick_once(now=now) == 1

    assert [e.task.id for e in fired] == ["apr20"]
