"""Warm the GFS capability cache once at startup, off the request path.

Which connection servers relay household envelopes (the signed
``envelope_relay`` capability on ``GET /gfs/info``) is cached in RAM by
:class:`~socialhome.services.gfs_connection_service.GfsConnectionService`.
After a restart the cache is cold, and the only thing that warmed it was the
GFS WebSocket's (re)connect hook — so the connections list read
``envelope_relay: false`` (hiding the pairing reach picker and the GFS
fallback switch) until that socket came up. This runs one
:meth:`~socialhome.services.gfs_connection_service.GfsConnectionService
.warm_capabilities` pass in the background as soon as the app starts,
independent of the socket.

One pass, no loop: a server that is down now is warmed by the WebSocket's
reconnect hook the moment it is back. The ``_stop`` event follows the
scheduler lifecycle (``replay_cache_scheduler.py``) so ``stop()`` lands
between two fetches instead of cancelling one mid-request.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

#: ``warm_capabilities(should_stop=...)`` → connections known relay-capable.
WarmFn = Callable[..., Awaitable[int]]


class GfsCapabilityWarmup:
    """One background ``/gfs/info`` pass over the active GFS connections."""

    __slots__ = ("_warm", "_task", "_stop")

    def __init__(self, warm: WarmFn) -> None:
        self._warm = warm
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        """Start the pass in the background. Idempotent; never blocks."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="gfs-capability-warmup")

    async def stop(self) -> None:
        """Stop after the fetch in flight and wait for the task to exit."""
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        try:
            relaying = await self._warm(should_stop=self._stop.is_set)
        except Exception:  # noqa: BLE001 — never take startup down
            log.warning("gfs: capability warm-up failed", exc_info=True)
            return
        log.info("gfs: capability cache warmed (%d relay-capable server(s))", relaying)


__all__ = ["GfsCapabilityWarmup"]
