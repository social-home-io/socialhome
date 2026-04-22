"""Periodic deadline notifier for tasks (§17.2 / §23.54).

Fires :class:`TaskDeadlineDue` for every task whose ``due_date`` is
today (or earlier, and not yet completed) that we haven't already
notified on that specific date. The repo's
``task_deadline_notifications`` table provides the dedupe key
``(task_id, due_date)`` so a restart never fires duplicates.

Same lifecycle pattern as :class:`PageLockExpiryScheduler` — an
``asyncio.Event`` drives the loop so ``app.py::_on_cleanup`` tears it
down cleanly.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

from ..domain.events import TaskDeadlineDue
from ..domain.task import TaskStatus

if TYPE_CHECKING:
    from ..db import AsyncDatabase
    from ..infrastructure.event_bus import EventBus
    from ..repositories.task_repo import AbstractTaskRepo

log = logging.getLogger(__name__)


class TaskDeadlineScheduler:
    """Background task that publishes ``TaskDeadlineDue`` events."""

    __slots__ = ("_repo", "_db", "_bus", "_interval", "_task", "_stop")

    def __init__(
        self,
        repo: "AbstractTaskRepo",
        db: "AsyncDatabase",
        bus: "EventBus",
        *,
        interval_seconds: float = 300.0,  # 5 min
    ) -> None:
        self._repo = repo
        self._db = db
        self._bus = bus
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                fired = await self.tick_once()
                if fired:
                    log.debug("task-deadline: fired %d", fired)
            except Exception as exc:  # pragma: no cover
                log.warning("task-deadline tick failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except asyncio.TimeoutError:
                continue

    async def tick_once(self, *, today: date | None = None) -> int:
        """Run one sweep. Returns the count of events published."""
        today = today or datetime.now(timezone.utc).date()
        due_today = await self._repo.list_due_on(today)
        fired = 0
        for task in due_today:
            if task.status == TaskStatus.DONE:
                continue
            if await self._already_notified(task.id, today):
                continue
            await self._bus.publish(
                TaskDeadlineDue(
                    task=task,
                    due_date=today,
                )
            )
            await self._record_notification(task.id, today)
            fired += 1
        return fired

    async def _already_notified(self, task_id: str, due: date) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM task_deadline_notifications WHERE task_id=? AND due_date=?",
            (task_id, due.isoformat()),
        )
        return row is not None

    async def _record_notification(self, task_id: str, due: date) -> None:
        await self._db.enqueue(
            "INSERT OR IGNORE INTO task_deadline_notifications"
            "(task_id, due_date) VALUES(?, ?)",
            (task_id, due.isoformat()),
        )
