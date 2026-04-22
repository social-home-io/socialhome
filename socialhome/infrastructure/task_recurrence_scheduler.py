"""Periodic spawn of overdue recurring tasks (§15 / §23.54).

A recurring task's follow-up normally spawns the moment the user marks
it DONE (see :meth:`TaskService._spawn_recurrence`). If they never
complete, the series gets stuck. This scheduler fills the gap: once
per hour it asks the service for every recurring task whose due date
has passed without a follow-up and spawns the next instance.

Idempotency comes from the ``last_spawned_at`` column (filtered in
:meth:`AbstractTaskRepo.list_recurring_overdue`), so a restart won't
re-spawn the same series.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..services.task_service import TaskService

log = logging.getLogger(__name__)


class TaskRecurrenceScheduler:
    """Background task that spawns overdue recurring-task instances."""

    __slots__ = ("_svc", "_interval", "_task", "_stop")

    def __init__(
        self,
        service: "TaskService",
        *,
        interval_seconds: float = 3600.0,  # hourly
    ) -> None:
        self._svc = service
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
                spawned = await self._svc.spawn_overdue_recurrences()
                if spawned:
                    log.debug("task-recurrence: spawned %d", len(spawned))
            except Exception as exc:  # pragma: no cover
                log.warning("task-recurrence tick failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except asyncio.TimeoutError:
                continue
