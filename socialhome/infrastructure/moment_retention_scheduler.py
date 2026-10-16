"""Background scheduler that prunes moments past the 7-day cap.

The list-query already collapses visibility to 24h for non-followers
and 7d for followers, so the scheduler only needs to drop rows past
the absolute ``expires_at`` (= ``created_at + 7 days``). Reactions
cascade via the FK.

Lifecycle follows the project-wide ``_stop: asyncio.Event`` template
(see ``replay_cache_scheduler.py``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..services.moment_service import MomentService

log = logging.getLogger(__name__)


class MomentRetentionScheduler:
    """Periodically run :meth:`MomentService.expire_due`."""

    __slots__ = ("_interval", "_service", "_stop", "_task")

    def __init__(
        self,
        service: MomentService,
        *,
        interval_seconds: float = 3600.0,  # one pass per hour
    ) -> None:
        self._service = service
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        """Start the loop. Idempotent — repeated calls are no-ops."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Set the stop event and wait briefly for the loop to exit."""
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
                pruned = await self._service.expire_due()
                if pruned:
                    log.debug("moment-retention: pruned %d expired moments", pruned)
            except Exception as exc:  # pragma: no cover — defensive
                log.warning("moment-retention prune failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except TimeoutError:
                continue


__all__ = ["MomentRetentionScheduler"]
