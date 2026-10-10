"""Re-read every paired GFS's ``/gfs/info`` about once an hour, jittered.

``/gfs/info`` used to be read only at pairing, at startup (the capability
warm-up) and on each GFS WebSocket (re)connect. A household whose socket
stays up for days never saw an operator's change of the server's public id —
so after a cluster moved to one shared id, a household could stay on a
retired per-node id for good. This loop calls
:meth:`~socialhome.services.gfs_connection_service.GfsConnectionService
.refresh_all_metadata` every :data:`DEFAULT_INTERVAL_S` ± a random
:data:`DEFAULT_JITTER_S`, so households converge (they adopt a new id only
when the server signs that it ``replaces`` the pinned one) without all
fetching at the same moment.

Lifecycle per ``replay_cache_scheduler.py``: ``_stop`` event, set in
``stop()``, the loop waits on it between ticks.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

#: Seconds between two passes, before jitter.
DEFAULT_INTERVAL_S: float = 3600.0
#: Up to this many seconds earlier or later, uniformly.
DEFAULT_JITTER_S: float = 600.0

#: ``refresh(should_stop=...)`` → connections refreshed.
RefreshFn = Callable[..., Awaitable[int]]


class GfsInfoRefreshScheduler:
    """Periodic, jittered ``/gfs/info`` refresh of every active connection."""

    __slots__ = ("_refresh", "_interval", "_jitter", "_rand", "_task", "_stop")

    def __init__(
        self,
        refresh: RefreshFn,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        jitter_s: float = DEFAULT_JITTER_S,
        rand: Callable[[], float] = random.random,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be > 0")
        self._refresh = refresh
        self._interval = interval_s
        self._jitter = max(0.0, min(jitter_s, interval_s))
        self._rand = rand
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def next_delay(self) -> float:
        """The wait before the next pass: interval ± jitter."""
        return self._interval + (2 * self._rand() - 1) * self._jitter

    async def start(self) -> None:
        """Start the loop in the background. Idempotent; never blocks."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="gfs-info-refresh")

    async def stop(self) -> None:
        """Stop after the fetch in flight and wait for the task to exit."""
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
                await asyncio.wait_for(self._stop.wait(), timeout=self.next_delay())
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self._refresh(should_stop=self._stop.is_set)
            except Exception:  # noqa: BLE001 — a background self-heal
                log.warning("gfs: periodic /gfs/info refresh failed", exc_info=True)


__all__ = ["GfsInfoRefreshScheduler"]
