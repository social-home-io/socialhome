"""Periodic reminders for upcoming calendar events (§17.2 / §23.79).

For every user's household calendar, this scheduler scans events
starting in the next ``reminder_window_minutes`` minutes and creates a
notification-centre entry ("`<event>` in 10 min") exactly once per
event per user.

Dedupe state is in-memory: after a reminder fires we remember
``(event_id, user_id)`` for the lifetime of the scheduler. That's
sufficient for single-process deployments; a restart may re-fire a
reminder the first time the loop runs after boot, which is acceptable
(better to over-notify once than miss a reminder).

Mirrors the ``_stop: asyncio.Event`` lifecycle used across the other
schedulers (``ReplayCachePruneScheduler``, ``PageLockExpiryScheduler``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..repositories.calendar_repo import AbstractCalendarRepo
    from ..repositories.user_repo import AbstractUserRepo
    from ..services.notification_service import NotificationService

log = logging.getLogger(__name__)


class CalendarReminderScheduler:
    """Background task that pushes "upcoming event" notifications."""

    __slots__ = (
        "_calendar_repo",
        "_fired",
        "_interval",
        "_notif_service",
        "_stop",
        "_task",
        "_user_repo",
        "_window",
    )

    def __init__(
        self,
        calendar_repo: AbstractCalendarRepo,
        user_repo: AbstractUserRepo,
        notif_service: NotificationService,
        *,
        interval_seconds: float = 60.0,
        reminder_window_minutes: int = 10,
    ) -> None:
        self._calendar_repo = calendar_repo
        self._user_repo = user_repo
        self._notif_service = notif_service
        self._interval = interval_seconds
        self._window = timedelta(minutes=reminder_window_minutes)
        self._fired: set[tuple[str, str]] = set()
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
                    log.debug("calendar-reminder: fired %d", fired)
            except Exception as exc:  # pragma: no cover
                log.warning("calendar-reminder tick failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except TimeoutError:
                continue

    async def tick_once(self) -> int:
        """Run one reminder sweep. Exposed for tests.

        Returns the number of reminders fired this tick.
        """
        now = datetime.now(UTC)
        horizon = now + self._window
        users = await self._user_repo.list_active()
        fired = 0
        for user in users:
            try:
                events = await self._calendar_repo.list_events_for_user_in_range(
                    user.username,
                    start=now,
                    end=horizon,
                )
            except Exception as exc:  # pragma: no cover
                log.debug(
                    "calendar-reminder: list events failed user=%s: %s",
                    user.username,
                    exc,
                    exc_info=True,
                )
                continue
            for event in events:
                key = (event.id, user.user_id)
                if key in self._fired:
                    continue
                # Only fire when the event starts within the window —
                # ``list_events_for_user_in_range`` returns anything
                # overlapping the window, including ones that have
                # already started.
                if event.start < now or event.start > horizon:
                    continue
                minutes = int((event.start - now).total_seconds() // 60)
                # The service words it in the user's language.
                await self._notif_service.notify_upcoming_event(
                    user, event_id=event.id, summary=event.summary, minutes=minutes
                )
                self._fired.add(key)
                fired += 1
        return fired
