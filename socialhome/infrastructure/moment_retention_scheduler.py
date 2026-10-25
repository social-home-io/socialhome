"""Background scheduler that prunes moments past their ``expires_at``.

The list-query already collapses visibility to 24h for non-followers;
followers see a moment until its ``expires_at`` (the author's choice,
1–30 days, clamped to 30 on receipt), so the scheduler only needs to drop
rows past it. Reactions
cascade via the FK. A moment written here stays in its author's archive
(decision 6a); the same tick then enforces the household's per-person
archive cap (:meth:`MomentArchiveService.enforce_caps`, bounded work per
tick).

Lifecycle follows the project-wide ``_stop: asyncio.Event`` template
(see ``replay_cache_scheduler.py``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..services.moment_archive_service import MomentArchiveService
    from ..services.moment_service import MomentService

log = logging.getLogger(__name__)


class MomentRetentionScheduler:
    """Periodically run :meth:`MomentService.expire_due`, then
    :meth:`MomentArchiveService.enforce_caps`."""

    __slots__ = ("_archive", "_interval", "_service", "_stop", "_task")

    def __init__(
        self,
        service: MomentService,
        *,
        archive: MomentArchiveService | None = None,
        interval_seconds: float = 3600.0,  # one pass per hour
    ) -> None:
        self._service = service
        self._archive = archive
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
            except Exception as exc:
                log.warning("moment-retention prune failed: %s", exc, exc_info=True)
            if self._archive is not None:
                try:
                    await self._archive.enforce_caps()
                except Exception as exc:
                    log.warning(
                        "moment-retention archive cap failed: %s", exc, exc_info=True
                    )
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except TimeoutError:
                continue


__all__ = ["MomentRetentionScheduler"]
