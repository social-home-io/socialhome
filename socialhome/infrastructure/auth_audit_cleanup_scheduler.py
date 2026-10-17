"""Periodic GC for ``auth_audit_log``.

Every login attempt (success and failure), every admin-issued password
reset, and every redeem writes a row. The table is an append-only trail,
so without pruning it grows forever — and it's attacker-drivable: an
unauthenticated brute-forcer appends one row per failed login and can
fill the DB. This scheduler runs once per ``interval_seconds`` and drops
any row older than the repo's retention window (90 days — plenty of
history for spotting brute force).

Pattern matches ``replay_cache_scheduler.ReplayCachePruneScheduler``.
"""

from __future__ import annotations

import asyncio
import logging

from ..repositories.auth_audit_log_repo import AbstractAuthAuditLogRepo

log = logging.getLogger(__name__)


class AuthAuditCleanupScheduler:
    """Background task that drops aged auth-audit rows."""

    __slots__ = ("_interval", "_repo", "_stop", "_task")

    def __init__(
        self,
        repo: AbstractAuthAuditLogRepo,
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
                        "auth-audit cleanup: pruned %d aged rows",
                        pruned,
                    )
            except Exception as exc:  # pragma: no cover
                log.warning("auth-audit cleanup failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except TimeoutError:
                continue

    async def _prune_once(self) -> int:
        """Run one prune pass. Exposed for tests."""
        return await self._repo.prune_older_than()
