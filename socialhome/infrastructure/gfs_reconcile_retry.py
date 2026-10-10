"""Bounded retry of the GFS reconnect reconcile behind the possession gate.

On each GFS WebSocket (re)connect the household runs an identity-bound
reconcile (pin heal, seat re-subscribe, epoch notices, name push, content-key
reconcile) — for a connection whose server once proved possession of its key
(the ratchet) only after a fresh proof
(:meth:`~socialhome.services.gfs_connection_service.GfsConnectionService
.possession_gate`). A RETRYABLE answer to that proof — a rolling deploy's
404, a balancer's 503, a 429, no answer — must not cost the reconcile for the
life of the socket. This keeps such a connection *pending* and re-runs
gate + reconcile after :data:`DEFAULT_RETRY_DELAYS_S` (15 s, 1 min, 5 min);
past those it stays pending and the hourly ``/gfs/info`` pass
(:class:`~socialhome.infrastructure.gfs_info_refresh_scheduler
.GfsInfoRefreshScheduler`) calls :meth:`GfsReconcileRetry.run_pending`.
A verified signature failure ends it without a reconcile.

The run callback answers ``"done"`` (gate passed, reconcile ran), ``"retry"``
(still retryable) or ``"skip"`` (refused — stop). Lifecycle per
``replay_cache_scheduler.py``: a ``_stop`` event, set in :meth:`stop`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

#: Waits before each retry of a pending connection's gate + reconcile.
DEFAULT_RETRY_DELAYS_S: tuple[float, ...] = (15.0, 60.0, 300.0)
#: What :meth:`GfsReconcileRetry.schedule` uses — read per schedule (a test
#: seam; production keeps the default).
RETRY_DELAYS_S: tuple[float, ...] = DEFAULT_RETRY_DELAYS_S

#: ``run(gfs_id)`` → ``"done"`` | ``"retry"`` | ``"skip"``.
RunFn = Callable[[str], Awaitable[str]]


class GfsReconcileRetry:
    """Per-connection bounded retry of gate + reconcile."""

    __slots__ = ("_run", "_pending", "_tasks", "_stop")

    def __init__(self, run: RunFn) -> None:
        self._run = run
        self._pending: set[str] = set()
        self._tasks: dict[str, asyncio.Task] = {}
        self._stop = asyncio.Event()

    def pending(self) -> set[str]:
        """Connections whose reconcile is still owed."""
        return set(self._pending)

    def schedule(self, gfs_id: str) -> None:
        """Mark *gfs_id* pending and start its retry loop (one per id)."""
        if self._stop.is_set():
            return
        self._pending.add(gfs_id)
        task = self._tasks.get(gfs_id)
        if task is not None and not task.done():
            return
        self._tasks[gfs_id] = asyncio.create_task(
            self._loop(gfs_id, RETRY_DELAYS_S), name=f"gfs-reconcile-retry-{gfs_id}"
        )

    def cancel(self, gfs_id: str) -> None:
        """Forget *gfs_id* (its reconcile ran some other way, or it is gone)."""
        self._pending.discard(gfs_id)
        task = self._tasks.pop(gfs_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def run_pending(self) -> int:
        """One attempt for every pending connection without a running loop —
        the hourly pass. Returns how many completed."""
        done = 0
        for gfs_id in sorted(self._pending):
            if self._stop.is_set():
                break
            task = self._tasks.get(gfs_id)
            if task is not None and not task.done():
                continue
            if await self._attempt(gfs_id):
                done += 1
        return done

    async def wait_idle(self) -> None:
        """Wait for every running loop (tests)."""
        tasks = [t for t in self._tasks.values() if not t.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        """Stop every loop (app cleanup); later schedules are ignored."""
        self._stop.set()
        tasks = [t for t in self._tasks.values() if not t.done()]
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=5.0
                )
            except asyncio.TimeoutError:
                for t in tasks:
                    t.cancel()
        self._tasks.clear()
        self._pending.clear()

    async def _loop(self, gfs_id: str, delays: tuple[float, ...]) -> None:
        for delay in delays:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass
            if gfs_id not in self._pending:
                return
            if await self._attempt(gfs_id):
                return

    async def _attempt(self, gfs_id: str) -> bool:
        """One gate + reconcile; ``True`` when it is no longer pending."""
        try:
            result = await self._run(gfs_id)
        except Exception:  # noqa: BLE001 — a background self-heal
            log.warning("gfs: reconcile retry failed for %s", gfs_id, exc_info=True)
            return False
        if result in ("done", "skip"):
            self._pending.discard(gfs_id)
            return True
        return False


__all__ = ["DEFAULT_RETRY_DELAYS_S", "GfsReconcileRetry"]
