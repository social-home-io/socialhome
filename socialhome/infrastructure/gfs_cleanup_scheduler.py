"""Periodic retry of what a GFS still owes this household after a space
key rotation.

When the owner rotates a space's authority key (an admin household lost the
signing seed), every GFS that holds state tied to the old key must follow:
the pin of a listing, the pin a GFS keeps for a listing the owner withdrew,
and a private space's opaque channel. Each of those is one request that can
fail — the server is restarting, answers 5xx, or is an older build. Until
it lands, the retired key still works on that server.

The requests themselves are sent by the rotation, and again on every GFS
reconnect. This scheduler is the third leg: a server that stayed connected
but refused once gets no reconnect, so a pass runs every
``interval_seconds`` and re-sends what is still owed. What is owed is read
from the database (and the per-process confirmation map), so a pass with
nothing owed sends nothing — the timer is a retry clock, not a poll of any
server.

Lifecycle follows the scheduler pattern (``_stop: asyncio.Event``, see
``replay_cache_scheduler.py``). The first pass waits one interval: the
reconnect heal already covers a start.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence

log = logging.getLogger(__name__)

#: Seconds between passes. Long enough that a dead server costs a handful of
#: requests an hour, short enough that a restarted one follows a rotation
#: within minutes.
GFS_CLEANUP_INTERVAL_S: float = 300.0

#: One named step of a pass; returns how many items it still owes (or any
#: count — only logged).
CleanupStep = tuple[str, Callable[[], Awaitable[int]]]


class GfsCleanupScheduler:
    """Background task re-sending owed GFS re-pins and channel retires."""

    __slots__ = ("_interval", "_steps", "_stop", "_task")

    def __init__(
        self,
        steps: Sequence[CleanupStep],
        *,
        interval_seconds: float = GFS_CLEANUP_INTERVAL_S,
    ) -> None:
        self._steps = tuple(steps)
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
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                await self.run_once()

    async def run_once(self) -> int:
        """Run every step once, fail-soft per step. Returns the total the
        steps reported. Exposed for tests."""
        total = 0
        for name, step in self._steps:
            try:
                count = int(await step() or 0)
            except Exception:
                log.exception("gfs cleanup: step %s failed", name)
                continue
            if count:
                log.info("gfs cleanup: %s — %d", name, count)
            total += count
        return total
