"""Periodic GC for fully-left DM conversations (§23.47c).

When every local member of a 1:1 DM or group DM has soft-left
(``conversation_members.deleted_at`` set on every row) and no remote
members are attached, the conversation is empty. The spec says a
background job hard-deletes those rows; ``ON DELETE CASCADE`` on the
child tables takes care of messages, reactions, delivery state, gap
rows, etc.

Federated conversations are skipped by ``list_fully_left_conversation_ids``
— their lifecycle is owned by the federation peer, not the local GC.
"""

from __future__ import annotations

import asyncio
import logging

from ..repositories.conversation_repo import AbstractConversationRepo

log = logging.getLogger(__name__)


class DmGcScheduler:
    """Background task that hard-deletes fully-left DM conversations."""

    __slots__ = ("_repo", "_interval", "_task", "_stop")

    def __init__(
        self,
        repo: AbstractConversationRepo,
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
            except asyncio.TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                pruned = await self._sweep_once()
                if pruned:
                    log.debug("dm-gc: hard-deleted %d empty conversations", pruned)
            except Exception as exc:  # pragma: no cover
                log.warning("dm-gc sweep failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except asyncio.TimeoutError:
                continue

    async def _sweep_once(self) -> int:
        """Run one sweep pass. Exposed for tests."""
        ids = await self._repo.list_fully_left_conversation_ids()
        for conversation_id in ids:
            await self._repo.hard_delete(conversation_id)
        return len(ids)
