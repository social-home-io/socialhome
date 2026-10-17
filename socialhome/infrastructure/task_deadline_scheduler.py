"""Periodic deadline notifier for tasks (§17.2 / §23.54).

Fires :class:`TaskDeadlineDue` for every open task whose ``due_date``
is today — the household-local date (``HouseholdPreferences.tz``) —
that we haven't already notified on that specific date. The sweep runs
every few minutes, so a task notifies shortly after local midnight. The repo's
``task_deadline_notifications`` table provides the dedupe key
``(task_id, due_date)`` so a restart never fires duplicates.

Same lifecycle pattern as :class:`PageLockExpiryScheduler` — an
``asyncio.Event`` drives the loop so ``app.py::_on_cleanup`` tears it
down cleanly.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from ..domain.events import TaskDeadlineDue
from ..domain.task import TaskStatus
from ..utils.timezones import DEFAULT_TZ, local_date

if TYPE_CHECKING:
    from ..db import AsyncDatabase
    from ..infrastructure.event_bus import EventBus
    from ..repositories.task_repo import AbstractTaskRepo
    from ..services.preferences_service import PreferencesService

log = logging.getLogger(__name__)

#: Dedupe rows only guard same-day refires, so anything older than this
#: window is dead weight. Pruned at the end of each daily ``tick_once``.
DEADLINE_NOTIF_RETENTION_DAYS = 30


class TaskDeadlineScheduler:
    """Background task that publishes ``TaskDeadlineDue`` events."""

    __slots__ = ("_bus", "_db", "_interval", "_preferences", "_repo", "_stop", "_task")

    def __init__(
        self,
        repo: AbstractTaskRepo,
        db: AsyncDatabase,
        bus: EventBus,
        *,
        preferences: PreferencesService | None = None,
        interval_seconds: float = 300.0,  # 5 min
    ) -> None:
        self._repo = repo
        self._db = db
        self._bus = bus
        self._preferences = preferences
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
            except TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                fired = await self.tick_once()
                if fired:
                    log.debug("task-deadline: fired %d", fired)
            except Exception as exc:  # pragma: no cover
                log.warning("task-deadline tick failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except TimeoutError:
                continue

    async def tick_once(
        self,
        *,
        today: date | None = None,
        now: datetime | None = None,
    ) -> int:
        """Run one sweep. Returns the count of events published.

        "Today" is the household-local date (``HouseholdPreferences.tz``),
        not the UTC one — otherwise a household far from UTC is told
        about tomorrow's tasks in its evening (west) or about today's
        half a day late (east). The dedupe + prune keys use the same
        household date, so each task notifies once per local day.
        """
        if today is None:
            instant = now or datetime.now(UTC)
            today = local_date(instant, await self._household_tz())
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
        await self._prune_old(today)
        return fired

    async def _household_tz(self) -> str:
        if self._preferences is None:
            return DEFAULT_TZ
        try:
            return (await self._preferences.get_household()).tz
        except Exception as exc:  # defensive — fall back to UTC
            log.warning("task-deadline: household tz failed: %s", exc, exc_info=True)
            return DEFAULT_TZ

    async def _prune_old(self, today: date) -> None:
        """Drop dedupe rows older than the retention window (best-effort)."""
        cutoff = (today - timedelta(days=DEADLINE_NOTIF_RETENTION_DAYS)).isoformat()
        await self._db.enqueue(
            "DELETE FROM task_deadline_notifications WHERE due_date < ?",
            (cutoff,),
        )

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
