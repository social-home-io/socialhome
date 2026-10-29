"""Periodic pruning for ``app_pending_sessions``.

The ``app_pending_sessions`` table durably keeps the app sessions a person
has — invites waiting in Needs you and games being played, with the latest
message their app hasn't taken yet (the mailbox, 0131). The repo caps a
single (app, user) pair, but invites nobody plays and games nobody moves in
would otherwise stay on disk forever.

This scheduler runs once per ``interval_seconds`` and deletes rows quiet for
longer than the TTL via the repo's ``prune_pending_sessions``. Hourly is plenty — the volume
is tiny and the per-pair cap already bounds the worst case.
"""

from __future__ import annotations

import asyncio
import logging

from ..repositories.app_repo import AbstractAppRepo

log = logging.getLogger(__name__)


class AppPendingSessionPruneScheduler:
    """Background task that prunes TTL-expired pending app-session invites."""

    __slots__ = ("_interval", "_repo", "_stop", "_task")

    def __init__(
        self,
        repo: AbstractAppRepo,
        *,
        interval_seconds: float = 3600.0,  # hourly
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
                    log.debug("app-pending-sessions: pruned %d stale rows", pruned)
            except Exception as exc:  # pragma: no cover
                log.warning("app-pending-sessions prune failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except TimeoutError:
                continue

    async def _prune_once(self) -> int:
        """Run one prune pass. Exposed for tests."""
        return await self._repo.prune_pending_sessions()
