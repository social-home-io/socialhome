"""Periodic sweep that clears user statuses past their ``expires_at``.

A status set with "Clear after 1h" stores an ``expires_at``; reads
already hide an expired status, but something has to *clear* it so the
``user.status_changed`` frame reaches open tabs and ``USER_STATUS_UPDATED``
reaches paired households. This scheduler runs
:meth:`UserService.clear_expired_statuses` once per ``interval_seconds``.

Pattern matches ``replay_cache_scheduler.ReplayCachePruneScheduler``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..services.user_service import UserService

log = logging.getLogger(__name__)


class UserStatusExpiryScheduler:
    """Background task that clears expired user statuses."""

    __slots__ = ("_users", "_interval", "_task", "_stop")

    def __init__(
        self,
        user_service: "UserService",
        *,
        interval_seconds: float = 60.0,
    ) -> None:
        self._users = user_service
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
                cleared = await self.run_once()
                if cleared:
                    log.debug("user-status expiry: cleared %d statuses", cleared)
            except Exception as exc:
                log.warning("user-status expiry sweep failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue

    async def run_once(self) -> int:
        """Run one sweep. Exposed for tests."""
        return await self._users.clear_expired_statuses()
