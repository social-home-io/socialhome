"""Background scheduler that prunes expired / over-quota highlights.

Two passes per tick:

1. Drop highlights whose ``expires_at`` lies in the past — the cutoff is
   set per-author by their ``preferences_json["highlights"].retention_days``
   when the highlight is first created.
2. For each author with highlights, count rows and drop the oldest beyond
   their ``preferences_json["highlights"].max_count`` ceiling — even if
   they have not yet expired.

Cascades through the FK chain delete the frames, views, and reactions
attached to a removed highlight. Share-cards in the household feed have
``ON DELETE SET NULL`` on ``feed_posts.linked_highlight_id`` so they degrade
to a "Highlight has ended" placeholder rather than disappearing.

Lifecycle follows the project-wide ``_stop: asyncio.Event`` template
(see ``replay_cache_scheduler.py``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..services.highlight_service import HighlightService

log = logging.getLogger(__name__)


class HighlightRetentionScheduler:
    """Periodically run :meth:`HighlightService.expire_due`."""

    __slots__ = ("_interval", "_service", "_stop", "_task")

    def __init__(
        self,
        service: HighlightService,
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
                expired, over_max = await self._service.expire_due()
                if expired or over_max:
                    log.debug(
                        "highlight-retention: pruned %d expired, %d over-max",
                        expired,
                        over_max,
                    )
            except Exception as exc:  # pragma: no cover — defensive
                log.warning("highlight-retention prune failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except TimeoutError:
                continue


__all__ = ["HighlightRetentionScheduler"]
