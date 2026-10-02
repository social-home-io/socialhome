"""Hourly sweep of the space moderation queue (§4.3 ``MODERATED``).

Two jobs, both on :class:`~socialhome.services.space_moderation_service.SpaceModerationService`:

* **expire** — a pending item nobody decided within its review window
  (``expires_at`` = submitted + 7 days) becomes ``expired``; the submitter
  is told (``moderation_decided`` notification + realtime frame).
* **purge** — decided / expired items older than 7 days lose their
  ``payload_json`` / ``current_snapshot``: the row stays for audit, the
  submitted words do not.

Follows the ``asyncio.Event`` lifecycle of
:mod:`~socialhome.infrastructure.replay_cache_scheduler`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

log = logging.getLogger(__name__)


class _ModerationSweeper(Protocol):
    async def expire_due(self) -> int: ...
    async def purge_decided(self) -> int: ...


class ModerationExpiryScheduler:
    """Background task that expires and purges moderation-queue items."""

    __slots__ = ("_svc", "_interval", "_task", "_stop")

    def __init__(
        self,
        moderation_service: _ModerationSweeper,
        *,
        interval_seconds: float = 3600.0,  # hourly
    ) -> None:
        self._svc = moderation_service
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
                await self.run_once()
            except Exception as exc:  # pragma: no cover - logged, loop survives
                log.warning("moderation expiry sweep failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue

    async def run_once(self) -> tuple[int, int]:
        """One sweep: (expired, purged). Exposed for tests."""
        expired = await self._svc.expire_due()
        purged = await self._svc.purge_decided()
        if expired or purged:
            log.info(
                "moderation queue: expired %d item(s), purged %d payload(s)",
                expired,
                purged,
            )
        return expired, purged
