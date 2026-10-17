"""Periodic GC for ``notifications``.

The notification centre (§17.2) caps each user at the most recent
``MAX_PER_USER`` rows on insert, but a user who never receives a fresh
notification keeps their old rows forever — and inactive accounts leave
aged rows behind indefinitely. This scheduler runs once per
``interval_seconds`` and drops any row older than the repo's 90-day
retention window.

Pattern matches ``auth_audit_cleanup_scheduler.AuthAuditCleanupScheduler``.
"""

from __future__ import annotations

import asyncio
import logging

from ..repositories.notification_repo import AbstractNotificationRepo

log = logging.getLogger(__name__)


class NotificationCleanupScheduler:
    """Background task that drops aged notification rows."""

    __slots__ = ("_interval", "_repo", "_stop", "_task")

    def __init__(
        self,
        repo: AbstractNotificationRepo,
        *,
        interval_seconds: float = 3600.0,  # once per hour
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
            except TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                pruned = await self._prune_once()
                if pruned:
                    log.debug(
                        "notification cleanup: pruned %d aged rows",
                        pruned,
                    )
            except Exception as exc:  # pragma: no cover
                log.warning("notification cleanup failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except TimeoutError:
                continue

    async def _prune_once(self) -> int:
        """Run one prune pass. Exposed for tests."""
        return await self._repo.delete_old()
