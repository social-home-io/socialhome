"""Background scheduler that empties the household gallery trash.

A deleted household photo or video stays in the trash for
:data:`~socialhome.domain.gallery.TRASH_RETENTION_DAYS` (migration 0139);
once a pass finds it older than that, :meth:`GalleryService.purge_expired_trash`
deletes the row (its favourites go with it) and the files no other row
names, through ``aiofiles``. Each pass is bounded; a backlog drains over the
following ticks.

Lifecycle follows the project-wide ``_stop: asyncio.Event`` template
(see ``replay_cache_scheduler.py``): a stop never cancels a pass halfway
through a write.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..services.gallery_service import GalleryService

log = logging.getLogger(__name__)


class GalleryTrashPurgeScheduler:
    """Periodically run :meth:`GalleryService.purge_expired_trash`."""

    __slots__ = ("_interval", "_service", "_stop", "_task")

    def __init__(
        self,
        service: GalleryService,
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
                released = await self._service.purge_expired_trash()
                if released:
                    log.debug("gallery-trash: released %d media references", released)
            except Exception as exc:
                log.warning("gallery-trash purge failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue
