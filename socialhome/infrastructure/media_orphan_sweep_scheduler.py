"""Periodic scheduler driving the media orphan sweep.

Backstop for media files whose owning DB row was removed without the
per-delete cleanup running (most notably remote-originated deletes via
federation inbound). Delegates the actual work to
:class:`MediaOrphanSweepService`; this class only owns the timing.

Follows the canonical ``_stop: asyncio.Event`` scheduler lifecycle.
"""

from __future__ import annotations

import asyncio
import logging

from ..services.media_orphan_sweep_service import MediaOrphanSweepService

log = logging.getLogger(__name__)


class MediaOrphanSweepScheduler:
    """Background task that periodically sweeps orphaned media files."""

    __slots__ = ("_interval", "_service", "_stop", "_task")

    def __init__(
        self,
        service: MediaOrphanSweepService,
        *,
        interval_seconds: float = 6 * 60 * 60,  # every 6 hours
    ) -> None:
        self._service = service
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
                await self._service.sweep_once()
                # The top-level sweep skips the ``transcode_src/`` subdir; its
                # leaked source blobs are reaped by a sibling pass each tick.
                await self._service.sweep_transcode_src_once()
            except Exception as exc:  # pragma: no cover — defensive
                log.warning("media-sweep failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue
