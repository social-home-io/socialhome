"""Background video-transcode scheduler.

Converts the previously-synchronous upload-time video transcode into a
background job. The upload handler stashes the raw source bytes in a
temp file, writes one ``media_transcode_jobs`` row, and returns the
future ``output_filename`` immediately so the SPA renders a
"processing" placeholder. This scheduler drains the table:

1. **On start** — :meth:`AbstractMediaTranscodeRepo.reclaim` flips any
   ``processing`` rows orphaned by a previous crash back to ``pending``
   so they're picked up again.
2. **Per tick** — :meth:`flush_once` claims each due row
   (``mark_processing``), reads the source bytes off disk, transcodes
   them to a VP9/Opus ``.webm`` plus a WebP poster via
   :class:`VideoProcessor`, writes both under the media root, deletes
   the row (readiness == absent row), removes the temp source, and
   publishes a :class:`MediaTranscodeReady` event.
3. **On failure** — transient failures reschedule with jittered
   exponential backoff up to :data:`MAX_ATTEMPTS`; at the cap the row
   flips to ``status='failed'`` so the repo's ``status_for`` surfaces
   ``'failed'`` to the SPA on its next list fetch, and we publish a
   :class:`MediaTranscodeFailed` event so the realtime layer can push a
   ``media.failed`` frame to the uploader for an immediate placeholder
   flip (mirrors the success-path :class:`MediaTranscodeReady`).

Mirrors :class:`socialhome.services.dm_media_sync_service.DmMediaSyncService`'s
scheduler lifecycle: an ``asyncio.Event``-based ``_stop`` / ``_wake``
loop, idempotent ``start`` / ``stop``, and a ``flush_once`` exposed for
unit tests + integration drivers.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import aiofiles
import aiofiles.os

from ..domain.events import MediaTranscodeFailed, MediaTranscodeReady
from ..media.video_processor import VideoProcessor
from ..repositories.media_transcode_repo import AbstractMediaTranscodeRepo
from .backoff import jittered_backoff_seconds
from .bus_publisher import BusPublisherMixin

if TYPE_CHECKING:
    from ..domain.media_transcode import MediaTranscodeJob
    from ..infrastructure.event_bus import EventBus


log = logging.getLogger(__name__)


#: Retry budget. After this many failed attempts a job flips to
#: ``status='failed'`` and ``status_for`` reports ``'failed'`` so the
#: SPA can render a "couldn't process this video" footnote.
MAX_ATTEMPTS: int = 3
#: Base backoff in seconds; each retry doubles up to ``BACKOFF_CAP``.
BACKOFF_BASE_SECONDS: float = 30.0
BACKOFF_CAP_SECONDS: float = 30 * 60.0


class MediaTranscodeService(BusPublisherMixin):
    """Drain ``media_transcode_jobs`` — transcode video in the background.

    The temp source file is removed on both success (``complete``) and
    permanent failure (``_fail`` at the attempt cap), so the normal paths
    never leak. A source blob can only orphan in the narrow crash window
    after the file is written but before its job row is processed — a
    periodic ``transcode_src`` sweep is a documented follow-up, not handled
    here (``reclaim`` only re-queues stuck ``processing`` rows).
    """

    __slots__ = (
        "_repo",
        "_media_dir",
        "_processor",
        "_bus",
        "_interval",
        "_task",
        "_stop",
        "_wake",
    )

    def __init__(
        self,
        *,
        repo: AbstractMediaTranscodeRepo,
        media_dir: pathlib.Path,
        processor: VideoProcessor,
        bus: "EventBus | None" = None,
        interval_seconds: float = 5.0,
    ) -> None:
        self._repo = repo
        self._media_dir = media_dir
        self._processor = processor
        self._bus = bus
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Set by ``nudge`` (the upload endpoints, after enqueue) so the
        # loop transcodes freshly-queued uploads immediately instead of
        # waiting up to ``interval_seconds`` for the next periodic tick.
        self._wake = asyncio.Event()

    def nudge(self) -> None:
        """Wake the loop so a freshly-enqueued job transcodes now.

        Called by the upload handlers after :meth:`enqueue` so the
        user's "processing" placeholder resolves promptly rather than
        on the next periodic poll.
        """
        self._wake.set()

    # ── Scheduler lifecycle ───────────────────────────────────────────

    async def start(self) -> None:
        """Start the background flush loop. Idempotent.

        Reclaims any ``processing`` rows orphaned by a previous crash
        before the loop's first tick — :meth:`list_due` filters those
        out, so without the reclaim they'd never retry.
        """
        if self._task is not None and not self._task.done():
            return
        try:
            stuck = await self._repo.reclaim()
            if stuck:
                log.info(
                    "media-transcode: reclaimed %d stuck processing row(s) "
                    "from a previous run",
                    stuck,
                )
        except Exception as exc:  # pragma: no cover — defensive
            log.warning("media-transcode: reclaim failed: %s", exc)
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the loop and wait for the task to exit."""
        self._stop.set()
        self._wake.set()  # break the loop's wake-wait so shutdown is prompt
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            # Clear before flushing so a nudge that races the flush still
            # leaves ``_wake`` set and the wait below returns at once
            # instead of losing the signal.
            self._wake.clear()
            try:
                done = await self.flush_once()
                if done:
                    log.debug("media-transcode: transcoded %d job(s)", done)
            except Exception as exc:  # pragma: no cover — defensive
                log.warning("media-transcode flush failed: %s", exc)
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue

    # ── Flush ──────────────────────────────────────────────────────────

    async def flush_once(self, *, limit: int = 5) -> int:
        """Run one transcode pass — claim due rows and process each.

        Returns the count of jobs that transcoded successfully. Exposed
        for unit tests + integration drivers (drive this directly to
        avoid waiting on the periodic tick).
        """
        due = await self._repo.list_due(limit=limit)
        done = 0
        for job in due:
            await self._repo.mark_processing(job.output_filename)
            source_path = pathlib.Path(job.source_path)
            if not await aiofiles.os.path.isfile(source_path):
                await self._fail(job, "source file missing")
                continue
            try:
                async with aiofiles.open(source_path, "rb") as f:
                    src = await f.read()
                webm_bytes, _ = await self._processor.process(src, "source")
                # ``generate_thumbnail`` extracts the first frame from the
                # *source* bytes as a WebP poster.
                thumb_bytes = await self._processor.generate_thumbnail(src)
                async with aiofiles.open(
                    self._media_dir / job.output_filename, "wb"
                ) as f:
                    await f.write(webm_bytes)
                async with aiofiles.open(
                    self._media_dir / job.thumbnail_filename, "wb"
                ) as f:
                    await f.write(thumb_bytes)
            except Exception as exc:
                log.warning(
                    "media-transcode: transcode failed for %s: %s",
                    job.output_filename,
                    exc,
                )
                await self._fail(job, str(exc))
                continue
            await self._repo.complete(job.output_filename)
            await self._remove_source(source_path)
            await self._publish_ready(job)
            done += 1
        return done

    # ── Internals ──────────────────────────────────────────────────────

    async def _fail(self, job: "MediaTranscodeJob", last_error: str) -> None:
        """Reschedule with jittered exponential backoff; fail at the cap.

        At :data:`MAX_ATTEMPTS` the row flips to ``status='failed'`` and
        the temp source is removed — the upload won't transcode and
        retrying further just burns CPU. Below the cap the row is
        rescheduled with equal-jitter backoff
        (:func:`~socialhome.services.backoff.jittered_backoff_seconds`) so
        a swarm of failing jobs doesn't all retry on the same tick, while
        no single job can land back in the due set on the next tick.
        """
        attempts = job.attempts + 1
        if attempts >= MAX_ATTEMPTS:
            await self._repo.mark_failed(job.output_filename, last_error[:500])
            await self._remove_source(pathlib.Path(job.source_path))
            await self._publish_failed(job)
            return
        delay = jittered_backoff_seconds(
            attempts=attempts,
            base_seconds=BACKOFF_BASE_SECONDS,
            cap_seconds=BACKOFF_CAP_SECONDS,
        )
        next_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        await self._repo.reschedule(
            job.output_filename,
            attempts=attempts,
            next_attempt_at=next_at.strftime("%Y-%m-%d %H:%M:%S"),
            last_error=last_error[:500],
        )

    async def _remove_source(self, source_path: pathlib.Path) -> None:
        """Best-effort delete the temp source bytes."""
        try:
            await aiofiles.os.remove(source_path)
        except OSError as exc:  # pragma: no cover — defensive
            log.debug(
                "media-transcode: could not remove temp source %s: %s",
                source_path,
                exc,
            )

    async def _publish_ready(self, job: "MediaTranscodeJob") -> None:
        """Fail-soft publish of :class:`MediaTranscodeReady` on success.

        Uses :meth:`BusPublisherMixin._emit` (no-ops when no bus is
        wired); wraps in a guard so a misbehaving subscriber never
        blocks the transcode pass from completing the row.
        """
        try:
            await self._emit(
                MediaTranscodeReady(
                    output_filename=job.output_filename,
                    thumbnail_filename=job.thumbnail_filename,
                    owner_user_id=job.owner_user_id,
                )
            )
        except Exception as exc:  # pragma: no cover — fail-soft
            log.debug(
                "media-transcode: ready-event publish failed for %s: %s",
                job.output_filename,
                exc,
            )

    async def _publish_failed(self, job: "MediaTranscodeJob") -> None:
        """Fail-soft publish of :class:`MediaTranscodeFailed` at the cap.

        Mirrors :meth:`_publish_ready` — uses :meth:`BusPublisherMixin._emit`
        (a no-op when no bus is wired) and guards the call so a misbehaving
        subscriber never blocks the fail path from completing.
        """
        try:
            await self._emit(
                MediaTranscodeFailed(
                    output_filename=job.output_filename,
                    owner_user_id=job.owner_user_id,
                )
            )
        except Exception as exc:  # pragma: no cover — fail-soft
            log.debug(
                "media-transcode: failed-event publish failed for %s: %s",
                job.output_filename,
                exc,
            )
