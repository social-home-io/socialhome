"""Hourly sweep that expires join requests nobody answered (v_84).

A pending request carries ``expires_at`` (7 days); until now nothing acted
on it, so a request nobody answered stayed pending for good — and a request
from the GFS held one of its space's capped slots
(:data:`~socialhome.federation.gfs_join_request.MAX_PENDING_PER_SPACE`)
forever. :meth:`SpaceService.expire_due_join_requests` marks them
``expired``.

Follows the ``asyncio.Event`` lifecycle of
:mod:`~socialhome.infrastructure.replay_cache_scheduler`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

log = logging.getLogger(__name__)


class _JoinRequestSweeper(Protocol):
    async def expire_due_join_requests(self) -> int: ...


class JoinRequestExpiryScheduler:
    """Background task that expires overdue join requests."""

    __slots__ = ("_interval", "_spaces", "_stop", "_task")

    def __init__(
        self,
        space_service: _JoinRequestSweeper,
        *,
        interval_seconds: float = 3600.0,  # hourly
    ) -> None:
        self._spaces = space_service
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
                await self.run_once()
            except Exception as exc:
                log.warning("join request expiry sweep failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue

    async def run_once(self) -> int:
        """One sweep; how many requests expired. Exposed for tests."""
        expired = await self._spaces.expire_due_join_requests()
        if expired:
            log.info("join requests: expired %d unanswered request(s)", expired)
        return expired
