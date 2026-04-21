"""Periodic release of stale page edit locks (§5.2 / §23.72).

Pages get an exclusive edit lock when a user begins editing. The lock
auto-expires after :data:`~social_home.repositories.page_repo.LOCK_TTL`
(60 s per spec) — clients refresh every 30 s — but the row only gets
cleared when someone *tries* to acquire or refresh. Without this
scheduler an abandoned editor tab leaves the lock visible to other
users until the next attempt, so we sweep aggressively (every 30 s)
for a deterministic "stale max" window.

Mirrors the start/stop pattern of :class:`ReplayCachePruneScheduler`.
"""

from __future__ import annotations

import asyncio
import logging

from ..repositories.page_repo import AbstractPageRepo

log = logging.getLogger(__name__)


class PageLockExpiryScheduler:
    """Background task that releases page locks past their TTL."""

    __slots__ = ("_repo", "_interval", "_task", "_stop")

    def __init__(
        self,
        repo: AbstractPageRepo,
        *,
        interval_seconds: float = 30.0,
    ) -> None:
        self._repo = repo
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
            except asyncio.TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                released = await self._repo.release_expired_locks()
                if released:
                    log.debug("page-lock: released %d expired locks", released)
            except Exception as exc:  # pragma: no cover
                log.warning("page-lock loop failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except asyncio.TimeoutError:
                continue
