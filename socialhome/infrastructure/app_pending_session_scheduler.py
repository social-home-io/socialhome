"""Periodic pruning for ``app_pending_sessions``.

The ``app_pending_sessions`` table durably keeps the app sessions a person
has — invites waiting in Needs you and games being played, with the latest
message their app hasn't taken yet (the mailbox, 0131). The repo caps a
single (app, user) pair, but invites nobody plays and games nobody moves in
would otherwise stay on disk forever.

This scheduler runs once per ``interval_seconds`` and deletes rows quiet for
longer than the TTL via the repo's ``prune_pending_sessions``. Hourly is plenty — the volume
is tiny and the per-pair cap already bounds the worst case.

With group tables wired (v_76) the same pass ends tables whose lobby ran
out and forgets tables that ended a week ago or went quiet
(:meth:`AppGroupService.sweep`).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ..repositories.app_repo import AbstractAppRepo

if TYPE_CHECKING:
    from ..services.app_group_service import AppGroupService

log = logging.getLogger(__name__)


class AppPendingSessionPruneScheduler:
    """Background task that prunes TTL-expired pending app-session invites."""

    __slots__ = ("_groups", "_interval", "_repo", "_stop", "_task")

    def __init__(
        self,
        repo: AbstractAppRepo,
        *,
        groups: AppGroupService | None = None,
        interval_seconds: float = 3600.0,  # hourly
    ) -> None:
        self._repo = repo
        self._groups = groups
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
        pruned = await self._repo.prune_pending_sessions()
        if self._groups is not None:
            pruned += await self._groups.sweep()
        return pruned
