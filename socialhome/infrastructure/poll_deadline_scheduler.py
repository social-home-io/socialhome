"""Close polls when their ``closes_at`` deadline passes (§9).

A poll with a deadline closes on its own: every ``interval_seconds`` this
sweep asks the household and the space :class:`PollService` to close the
polls whose ``closes_at`` has passed. Only the household that wrote a poll
closes it — a household poll is always local, a space poll only when a
local user wrote it (its member households take the author's household's
one ``SPACE_POLL_CLOSED``, the same close the author's button sends).

Each close is an atomic ``closed=0 → 1`` update, so a poll is announced
once (:class:`PollClosed` with ``at_deadline``) even when the author closes
it at the same moment; the ``closed`` flag is the only state, so a restart
picks up exactly the polls still open past their deadline.

Same ``_stop: asyncio.Event`` lifecycle as :mod:`.replay_cache_scheduler`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..services.poll_service import PollService

log = logging.getLogger(__name__)


class PollDeadlineScheduler:
    """Background task that closes polls past their deadline."""

    __slots__ = ("_interval", "_services", "_stop", "_task")

    def __init__(
        self,
        services: Sequence[PollService],
        *,
        interval_seconds: float = 60.0,
    ) -> None:
        self._services = tuple(services)
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        """Start the background loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the loop and wait for the task to exit."""
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
                closed = await self.tick_once()
                if closed:
                    log.debug("poll-deadline: closed %d", closed)
            except Exception as exc:  # pragma: no cover
                log.warning("poll-deadline sweep failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue

    async def tick_once(self, now: datetime | None = None) -> int:
        """One sweep over every poll service. Returns how many closed."""
        at = now or datetime.now(UTC)
        closed = 0
        for service in self._services:
            closed += await service.close_due_polls(at)
        return closed
