"""One upload batch, one event (household gallery D9).

A phone uploads a holiday one photo per request, so the gallery publishes
one :class:`GalleryItemUploaded` per photo. Telling people about each of
them would be 80 notification rows. This service coalesces them on the
server — nothing the client says decides what a batch is:

* a **batch** is everything one uploader adds to one album until they go
  quiet for :data:`GALLERY_BATCH_QUIET_SECONDS` (a slow phone upload of
  80 photos keeps the batch open; stopping for a coffee closes it);
* a batch that keeps going is cut after :data:`GALLERY_BATCH_MAX_SECONDS`,
  so a long upload still tells people while it runs (the next batch joins
  the same notification row while it is unread);
* then one :class:`GalleryItemsAdded` is published with how many photos
  and videos it held.

Uploads by another household into a shared space album arrive as
:class:`GalleryItemUploaded` too (the federation inbound re-publishes
them), so this household batches its own members' rows for them. The
system "Posts" album's mirror events pass through as well; the
notification handler drops them (a feed post already told people).

Pending batches live in memory: a restart loses at most one quiet window
of "new photos" rows (stop() flushes what is pending on a clean
shutdown). At most :data:`GALLERY_BATCH_MAX_PENDING` batches wait at once;
past it the oldest is published early.

The tick follows the scheduler pattern (``_stop: asyncio.Event``, see
``infrastructure/replay_cache_scheduler.py``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..domain.events import GalleryItemsAdded, GalleryItemUploaded
from ..infrastructure.event_bus import EventBus

log = logging.getLogger(__name__)

#: Quiet seconds that end a batch: no further upload by the same person to
#: the same album for this long.
GALLERY_BATCH_QUIET_SECONDS: float = 90.0
#: A batch still growing after this many seconds is published anyway.
GALLERY_BATCH_MAX_SECONDS: float = 600.0
#: Batches waiting at once, at most (one per uploader and album).
GALLERY_BATCH_MAX_PENDING: int = 500
#: How often the tick looks for batches that went quiet.
GALLERY_BATCH_TICK_SECONDS: float = 5.0


@dataclass(slots=True)
class _Batch:
    """Service-local: one open batch (mutable while it grows)."""

    space_id: str | None
    first_at: float
    last_at: float
    photos: int = 0
    videos: int = 0


class GalleryUploadBatcher:
    """Turns per-item upload events into one event per batch."""

    __slots__ = (
        "_bus",
        "_clock",
        "_max_seconds",
        "_pending",
        "_quiet_seconds",
        "_stop",
        "_task",
        "_tick_seconds",
    )

    def __init__(
        self,
        bus: EventBus,
        *,
        quiet_seconds: float = GALLERY_BATCH_QUIET_SECONDS,
        max_seconds: float = GALLERY_BATCH_MAX_SECONDS,
        tick_seconds: float = GALLERY_BATCH_TICK_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._quiet_seconds = quiet_seconds
        self._max_seconds = max_seconds
        self._tick_seconds = tick_seconds
        self._clock = clock
        self._pending: dict[tuple[str, str], _Batch] = {}
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def wire(self) -> None:
        self._bus.subscribe(GalleryItemUploaded, self._on_uploaded)

    @property
    def pending(self) -> int:
        """How many batches are open (for tests and diagnostics)."""
        return len(self._pending)

    async def _on_uploaded(self, event: GalleryItemUploaded) -> None:
        now = self._clock()
        key = (event.album_id, event.uploader)
        batch = self._pending.get(key)
        if batch is None:
            if len(self._pending) >= GALLERY_BATCH_MAX_PENDING:
                oldest = min(self._pending, key=lambda k: self._pending[k].first_at)
                await self._publish(oldest, self._pending.pop(oldest))
            batch = _Batch(space_id=event.space_id, first_at=now, last_at=now)
            self._pending[key] = batch
        batch.last_at = now
        if event.item_type == "video":
            batch.videos += 1
        else:
            batch.photos += 1

    async def flush_due(self) -> int:
        """Publish every batch that went quiet or ran too long; returns how
        many."""
        now = self._clock()
        due = [
            key
            for key, b in self._pending.items()
            if now - b.last_at >= self._quiet_seconds
            or now - b.first_at >= self._max_seconds
        ]
        for key in due:
            await self._publish(key, self._pending.pop(key))
        return len(due)

    async def flush_all(self) -> int:
        """Publish every open batch now (a clean shutdown)."""
        pending, self._pending = self._pending, {}
        for key, batch in pending.items():
            await self._publish(key, batch)
        return len(pending)

    async def _publish(self, key: tuple[str, str], batch: _Batch) -> None:
        album_id, uploader = key
        try:
            await self._bus.publish(
                GalleryItemsAdded(
                    album_id=album_id,
                    uploader=uploader,
                    photos=batch.photos,
                    videos=batch.videos,
                    space_id=batch.space_id,
                )
            )
        except Exception as exc:
            log.warning("gallery batch publish failed: %s", exc, exc_info=True)

    async def start(self) -> None:
        """Start the tick. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the tick, then publish what is still open."""
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None
        await self.flush_all()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.flush_due()
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("gallery batch tick failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick_seconds)
            except TimeoutError:
                continue
