"""Per-user calendar reminder scheduler (Phase D).

Polls ``space_calendar_rsvp_reminders`` on a fixed cadence (default 30 s)
for rows whose ``fire_at`` window has come due. For each, emits an
:class:`EventReminderDue` bus event and marks the row as sent. The
notification service subscribes to that event and delivers the push +
in-app notification.

Follows the standard scheduler template (``_stop: asyncio.Event``,
``while not self._stop.is_set()`` body, idempotent ``start`` / ``stop``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..domain.events import EventReminderDue
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..repositories.calendar_repo import AbstractSpaceCalendarRepo

log = logging.getLogger(__name__)


class SpaceCalendarReminderScheduler:
    """Periodic scheduler for due space-event RSVP reminders.

    Distinct from :class:`infrastructure.CalendarReminderScheduler`,
    which fires for personal calendars on a hardcoded 10-minute sweep.
    This scheduler is driven by user-configured per-event, per-occurrence
    reminders stored in ``space_calendar_rsvp_reminders``.
    """

    __slots__ = ("_repo", "_bus", "_interval", "_batch", "_task", "_stop")

    def __init__(
        self,
        *,
        calendar_repo: "AbstractSpaceCalendarRepo",
        bus: EventBus,
        interval_seconds: float = 30.0,
        batch_size: int = 100,
    ) -> None:
        self._repo = calendar_repo
        self._bus = bus
        self._interval = interval_seconds
        self._batch = batch_size
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
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None

    async def tick_once(self) -> int:
        """Fire any due reminders. Returns the number processed.

        Exposed for tests so we can drive the scheduler synchronously
        without sleeping.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        due = await self._repo.list_due_reminders(
            now_iso=now_iso, limit=self._batch,
        )
        if not due:
            return 0
        for r in due:
            evt = await self._repo.get_event(r.event_id)
            if evt is None:
                # Event vanished (deleted upstream) — drop the reminder.
                await self._repo.mark_reminder_sent(
                    event_id=r.event_id,
                    user_id=r.user_id,
                    occurrence_at=r.occurrence_at,
                    minutes_before=r.minutes_before,
                    sent_at=now_iso,
                )
                continue
            space_id, event = evt
            await self._bus.publish(
                EventReminderDue(
                    event_id=r.event_id,
                    user_id=r.user_id,
                    occurrence_at=r.occurrence_at,
                    minutes_before=r.minutes_before,
                    summary=event.summary,
                    space_id=space_id,
                )
            )
            await self._repo.mark_reminder_sent(
                event_id=r.event_id,
                user_id=r.user_id,
                occurrence_at=r.occurrence_at,
                minutes_before=r.minutes_before,
                sent_at=now_iso,
            )
        return len(due)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick_once()
            except Exception:
                log.exception("calendar reminder scheduler tick failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval,
                )
            except asyncio.TimeoutError:
                continue
