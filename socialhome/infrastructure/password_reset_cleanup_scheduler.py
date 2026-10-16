"""Periodic GC for ``password_reset_tokens``.

The reset path mints single-use tokens with a 1h TTL. Once used (or
once expired) they're useless, but the row stays as a tiny audit
trail. Without pruning, the table accumulates one row per reset
forever. This scheduler runs once per ``interval_seconds`` and
deletes any row whose ``expires_at`` has elapsed (covers both used
and unused tokens once they're past their hour).

Pattern matches ``replay_cache_scheduler.ReplayCachePruneScheduler``.
"""

from __future__ import annotations

import asyncio
import logging

from ..repositories.password_reset_repo import AbstractPasswordResetRepo

log = logging.getLogger(__name__)


class PasswordResetCleanupScheduler:
    """Background task that drops expired password-reset tokens."""

    __slots__ = ("_interval", "_repo", "_stop", "_task")

    def __init__(
        self,
        repo: AbstractPasswordResetRepo,
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
                        "password-reset cleanup: pruned %d expired rows",
                        pruned,
                    )
            except Exception as exc:  # pragma: no cover
                log.warning("password-reset cleanup failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except TimeoutError:
                continue

    async def _prune_once(self) -> int:
        """Run one prune pass. Exposed for tests."""
        return await self._repo.cleanup_expired()
