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
   (``mark_processing``) and converts it — one video at a time, file to
   file (a 2 GB phone video is never held in memory) — to a VP9/Opus
   ``.webm`` plus a WebP poster via :class:`VideoProcessor`. The WebM is
   written next to the source and moved into the media root when
   complete, so a half-written file is never served. Then it deletes
   the row (readiness == absent row), removes the temp source, and
   publishes a :class:`MediaTranscodeReady` event. A job keeps the
   length its upload was checked against (``duration_s``; the
   protocol's 60 s when it has none).
3. **On failure** — transient failures reschedule with jittered
   exponential backoff up to :data:`MAX_ATTEMPTS`; at the cap the row
   flips to ``status='failed'`` so the repo's ``status_for`` surfaces
   ``'failed'`` to the SPA on its next list fetch, and we publish a
   :class:`MediaTranscodeFailed` event so the realtime layer can push a
   ``media.failed`` frame to the uploader for an immediate placeholder
   flip (mirrors the success-path :class:`MediaTranscodeReady`).

**Time estimate** — :meth:`estimates` answers "ready in about 4 min" for
the "processing" tile: the seconds of video still ahead of a job (its
own included, the running one's progress subtracted) divided by how
fast this machine converts. That speed is learned from the jobs it has
finished (and the running one's progress), starting from a cautious
:data:`DEFAULT_SPEED`. It is never stored: after a restart the first job
re-learns it.

Mirrors :class:`socialhome.services.dm_media_sync_service.DmMediaSyncService`'s
scheduler lifecycle: an ``asyncio.Event``-based ``_stop`` / ``_wake``
loop, idempotent ``start`` / ``stop``, and a ``flush_once`` exposed for
unit tests + integration drivers.
"""

from __future__ import annotations

import asyncio
import logging
import math
import pathlib
import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiofiles
import aiofiles.os

from ..domain.events import MediaTranscodeFailed, MediaTranscodeReady
from ..domain.media_constraints import VIDEO_MAX_DURATION_SECONDS
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

#: Seconds of video converted per second before this machine has shown
#: its own speed. Cautious on purpose: measured 1080p → VP9 runs at
#: ~1 s/s on a desktop and ~8 s per second of video on a Raspberry Pi 4,
#: so the first estimate says "longer" rather than "sooner".
DEFAULT_SPEED: float = 0.25
#: Weight of the newest finished job in the learned speed.
SPEED_SMOOTHING: float = 0.5
#: The running job's own pace replaces the learned one once it has run
#: this long (early seconds are dominated by opening the file).
LIVE_SPEED_AFTER_S: float = 15.0
#: Speeds outside this band are a measuring artefact, not a machine.
_SPEED_BOUNDS: tuple[float, float] = (0.01, 50.0)


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
        "_active",
        "_active_done_s",
        "_active_started",
        "_bus",
        "_interval",
        "_media_dir",
        "_processor",
        "_repo",
        "_speed",
        "_stop",
        "_task",
        "_wake",
    )

    def __init__(
        self,
        *,
        repo: AbstractMediaTranscodeRepo,
        media_dir: pathlib.Path,
        processor: VideoProcessor,
        bus: EventBus | None = None,
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
        # The job being converted now, how far it got (seconds of video,
        # written from the worker thread) and since when.
        self._active: str | None = None
        self._active_done_s: float = 0.0
        self._active_started: float = 0.0
        # Seconds of video per second of conversion (learned).
        self._speed: float = DEFAULT_SPEED

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
            log.warning("media-transcode: reclaim failed: %s", exc, exc_info=True)
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the loop and wait for the task to exit."""
        self._stop.set()
        self._wake.set()  # break the loop's wake-wait so shutdown is prompt
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError, asyncio.CancelledError:
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
                log.warning("media-transcode flush failed: %s", exc, exc_info=True)
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
            except TimeoutError:
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
            # Written beside the source (never served), moved into the
            # media root only once complete.
            partial = source_path.with_name(f"{job.output_filename}.part")
            self._active = job.output_filename
            self._active_done_s = 0.0
            self._active_started = time.monotonic()
            try:
                await self._processor.transcode_file(
                    source_path,
                    partial,
                    max_duration_s=keep_seconds(job),
                    on_progress=self._on_progress,
                )
                # The poster is the first frame of the *source*.
                thumb_bytes = await self._processor.thumbnail_file(source_path)
                async with aiofiles.open(
                    self._media_dir / job.thumbnail_filename, "wb"
                ) as f:
                    await f.write(thumb_bytes)
                await aiofiles.os.replace(
                    partial, self._media_dir / job.output_filename
                )
            except Exception as exc:
                log.warning(
                    "media-transcode: transcode failed for %s: %s",
                    job.output_filename,
                    exc,
                    exc_info=True,
                )
                await self._remove_source(partial)
                await self._fail(job, str(exc))
                continue
            finally:
                self._active = None
            self._learn_speed(job, time.monotonic() - self._active_started)
            await self._repo.complete(job.output_filename)
            await self._remove_source(source_path)
            await self._publish_ready(job)
            done += 1
        return done

    # ── Time estimate ──────────────────────────────────────────────────

    async def estimates(self, output_filenames: Iterable[str]) -> dict[str, int]:
        """Seconds until each of *output_filenames* is ready (rough).

        Only queued jobs get an entry (a finished or failed one has
        nothing to wait for). Walks the queue in the order it runs: every
        job ahead adds its length, the running one only what it has left.
        """
        wanted = set(output_filenames)
        if not wanted:
            return {}
        queue = await self._repo.list_queue()
        speed = self._current_speed()
        out: dict[str, int] = {}
        ahead_s = 0.0
        for job in queue:
            length = keep_seconds(job)
            if job.output_filename == self._active:
                length = max(0.0, length - self._active_done_s)
            ahead_s += length / speed
            if job.output_filename in wanted:
                out[job.output_filename] = max(1, math.ceil(ahead_s))
                if len(out) == len(wanted):
                    break
        return out

    def _on_progress(self, done_s: float) -> None:
        """Worker-thread callback: seconds of the running video done."""
        self._active_done_s = done_s

    def _current_speed(self) -> float:
        """The learned speed, or the running job's own once it has run a
        while (a long video tells us more than the last short one)."""
        if self._active is not None:
            elapsed = time.monotonic() - self._active_started
            if elapsed >= LIVE_SPEED_AFTER_S and self._active_done_s > 0:
                return _bounded(self._active_done_s / elapsed)
        return self._speed

    def _learn_speed(self, job: MediaTranscodeJob, elapsed: float) -> None:
        """Fold a finished job's pace into the learned speed."""
        converted = self._active_done_s or (job.duration_s or 0.0)
        if elapsed <= 0 or converted <= 0:
            return
        sample = _bounded(converted / elapsed)
        self._speed = _bounded(
            SPEED_SMOOTHING * sample + (1 - SPEED_SMOOTHING) * self._speed
        )

    # ── Internals ──────────────────────────────────────────────────────

    async def _fail(self, job: MediaTranscodeJob, last_error: str) -> None:
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
        next_at = datetime.now(UTC) + timedelta(seconds=delay)
        await self._repo.reschedule(
            job.output_filename,
            attempts=attempts,
            next_attempt_at=next_at.strftime("%Y-%m-%d %H:%M:%S"),
            last_error=last_error[:500],
        )

    async def _remove_source(self, source_path: pathlib.Path) -> None:
        """Best-effort delete a temp file (the source, a partial WebM)."""
        try:
            await aiofiles.os.remove(source_path)
        except FileNotFoundError:
            return
        except OSError as exc:  # pragma: no cover — defensive
            log.debug(
                "media-transcode: could not remove temp source %s: %s",
                source_path,
                exc,
            )

    async def _publish_ready(self, job: MediaTranscodeJob) -> None:
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
                exc_info=True,
            )

    async def _publish_failed(self, job: MediaTranscodeJob) -> None:
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
                exc_info=True,
            )


def keep_seconds(job: MediaTranscodeJob) -> float:
    """How much of a job's video is kept: the length its upload was
    checked against (``duration_s``, whole seconds rounded up so the last
    frame stays), else the protocol's ``VIDEO_MAX_DURATION_SECONDS``."""
    if job.duration_s is not None and job.duration_s > 0:
        return float(math.ceil(job.duration_s))
    return float(VIDEO_MAX_DURATION_SECONDS)


def _bounded(speed: float) -> float:
    low, high = _SPEED_BOUNDS
    return min(high, max(low, speed))
