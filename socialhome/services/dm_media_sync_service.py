"""Cross-household DM media sync — the preview-now-sync-later flow.

Companion service to :class:`DmService`. When a DM with an image /
video / file attachment is sent to a directly-paired remote
household, this service:

1. **At send time** — builds a tiny preview (a ≤ 320 px WebP
   thumbnail for ``image``; a flat MIME glyph for ``video`` / ``file``
   v1, video poster generation lands in a follow-up) and asks
   :class:`DmService` to embed it inside the ``DM_MESSAGE`` envelope
   via ``preview_bytes_b64``. The receiver renders the preview
   *immediately* and flips ``media_sync_status='pending'`` on the
   row.
2. **In the background** — flushes the ``dm_media_outbox`` table
   the schema migration set up: pick a row, read the full bytes
   off disk, build a :data:`FederationEventType.DM_MEDIA_BLOB`
   envelope, dispatch through the existing
   :class:`FederationService`. On success the row is deleted; on
   failure the row is rescheduled with jittered exponential backoff
   up to a retry cap.
3. **At receive time** — the receiver's inbound handler decodes the
   bytes, persists them under the local media root, updates the
   matching ``conversation_messages`` row to point ``media_url`` at
   the full bytes + clear ``media_sync_status``, and publishes a
   ``dm.media_ready`` WebSocket frame. The sender doesn't act on
   this — the *recipient's* SPA swaps the bubble preview for the
   full media.

§25.8.21 encryption: the federation transport (HTTPS inbox or RTC
DataChannel) already wraps every envelope under the conversation
key. Embedding bytes inside ``DM_MESSAGE`` /
``DM_MEDIA_BLOB`` payloads doesn't add a second layer — it relies
on the same envelope crypto that protects ``content`` /
``media_url`` today.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import pathlib
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import aiofiles
import aiofiles.os

from ..domain.federation import FederationEventType
from ..media.image_processor import ImageProcessor
from ..media.video_processor import VideoProcessor
from .backoff import jittered_backoff_seconds
from .visibility import VisibilityMixin

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.conversation_repo import AbstractConversationRepo
    from ..repositories.dm_media_outbox_repo import AbstractDmMediaOutboxRepo
    from ..repositories.peer_user_visibility_repo import (
        AbstractPeerUserVisibilityRepo,
    )


log = logging.getLogger(__name__)


#: Longest-side cap for the inline preview embedded in
#: ``DM_MESSAGE``. 320 px is a tradeoff: large enough that a phone
#: photo reads as "yes, that's the picture I expect" while small
#: enough that a typical preview lands in 5–20 KB inside the
#: envelope — well under the federation transport's per-event budget.
PREVIEW_MAX_PX: int = 320
#: WebP quality factor for previews. Q60 sits squarely in the "good
#: enough for a thumbnail" band — the full bytes the receiver
#: eventually downloads via ``DM_MEDIA_BLOB`` are the high-quality
#: artifact, the preview just needs to identify the picture.
PREVIEW_WEBP_QUALITY: int = 60

#: Retry budget for the outbox scheduler. After this many failed
#: attempts the row flips to ``failed`` and the corresponding
#: ``conversation_messages.media_sync_status`` is moved to
#: ``'failed'`` so the SPA can render a "media couldn't be
#: delivered" footnote.
MAX_ATTEMPTS: int = 6
#: Base backoff in seconds; each retry doubles up to ``BACKOFF_CAP``.
BACKOFF_BASE_SECONDS: float = 30.0
BACKOFF_CAP_SECONDS: float = 30 * 60.0

#: Per-chunk raw byte cap for the ``DM_MEDIA_BLOB`` payload. The
#: federation transport (HTTPS inbox or RTC DataChannel) drops a frame
#: to the slow HTTPS fallback once a single serialised event exceeds
#: the ~1 MiB send budget (``transport.SEND_HWM_BYTES``). Base64 inflates
#: the raw bytes by ~37%, so 512 KiB lands at ~700 KB encoded — still
#: comfortably under the cap, while halving the chunk count (and the
#: per-chunk encrypt+sign passes) versus the old 256 KiB. 1 MiB raw would
#: inflate to ~1.37 MiB and blow the budget — the binary-framing follow-up
#: is what unlocks larger chunks. Files at or under
#: :data:`SINGLE_CHUNK_BYTES_THRESHOLD` still ride a single chunk; above
#: it the sender splits into N sequenced chunks and the receiver buffers
#: them on disk until ``final=true`` lands.
MAX_BLOB_CHUNK_BYTES: int = 512 * 1024
#: Max chunks of a single blob in flight at once. Pipelining cuts the
#: per-chunk round-trip latency (esp. on the HTTPS fallback). Kept small:
#: the RTC transport drops to HTTPS once its ~1 MiB send buffer fills, so a
#: large window can't overrun the DataChannel — it just backpressures.
PIPELINE_WINDOW: int = 4
#: Files at or below this size go through the single-chunk fast
#: path. Picked so typical phone photos / short clips (≤ ~1 MB)
#: never chunk — they're below the federation transport's
#: per-event budget already, and chunking just adds latency. A 200
#: MiB video, on the other hand, ships as ~410 chunks.
SINGLE_CHUNK_BYTES_THRESHOLD: int = 1024 * 1024


class DmMediaSyncService(VisibilityMixin):
    """Build previews, enqueue + flush ``DM_MEDIA_BLOB`` outbox rows."""

    __slots__ = (
        "_convos",
        "_outbox",
        "_federation",
        "_media_dir",
        "_image_proc",
        "_video_proc",
        "_interval",
        "_task",
        "_stop",
        "_wake",
    )

    def __init__(
        self,
        *,
        convos: AbstractConversationRepo,
        outbox: AbstractDmMediaOutboxRepo,
        federation: FederationService | None,
        media_dir: pathlib.Path,
        interval_seconds: float = 5.0,
        visibility_repo: "AbstractPeerUserVisibilityRepo | None" = None,
    ) -> None:
        self._convos = convos
        self._outbox = outbox
        self._federation = federation
        self._media_dir = media_dir
        self._visibility_repo = visibility_repo
        # One processor instance is fine — ``ImageProcessor`` is
        # stateless across calls (Pillow's Image objects are
        # short-lived per-call). ``VideoProcessor`` is the same shape
        # — its ``generate_thumbnail`` pulls the first frame and
        # encodes it as WebP, which we then re-resize down to the
        # preview cap below.
        self._image_proc = ImageProcessor()
        self._video_proc = VideoProcessor()
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Set by ``enqueue_for_message`` so the loop ships freshly-queued
        # media immediately instead of waiting up to ``interval_seconds``
        # for the next periodic tick. ``_interval`` stays as a fallback
        # poll (catches rows reclaimed after a crash / retries coming due).
        self._wake = asyncio.Event()

    def attach_federation(self, federation: "FederationService") -> None:
        """Wire federation after construction (breaks the boot cycle).

        :class:`FederationService` is built later than this service
        in :func:`create_app`. Stashing a setter here keeps the
        boot order linear — construct this with ``federation=None``
        and call :meth:`attach_federation` once the service exists.
        """
        self._federation = federation

    # ── Preview building ──────────────────────────────────────────────

    async def build_preview(
        self,
        *,
        media_url: str,
        kind: str,
        mime_type: str | None,
    ) -> str | None:
        """Build the inline preview for ``DM_MESSAGE.preview_bytes_b64``.

        ``kind`` is the message ``type``:

        * ``image`` — read the WebP off disk, downscale to
          :data:`PREVIEW_MAX_PX` @ :data:`PREVIEW_WEBP_QUALITY`.
        * ``video`` — extract the first frame via
          :class:`VideoProcessor`, then pipe through the same
          downscale path so a video bubble shows a recognisable
          poster on arrival (instead of the generic play-glyph
          placeholder until the full blob lands).
        * ``file`` — ``None``. Generic files have no inherent
          thumbnail; the receiver renders a paperclip glyph.

        Returns a base64-encoded WebP string ready to drop into the
        outbound payload, or ``None`` when no inline preview is
        available for this kind (or if extraction fails — the
        receiver falls back to the placeholder).
        """
        if kind == "file":
            return None
        path = await self._resolve_media_path(media_url)
        if path is None or not await aiofiles.os.path.isfile(path):
            log.debug(
                "dm-media-sync: preview source missing for %s",
                media_url,
            )
            return None
        try:
            async with aiofiles.open(path, "rb") as f:
                data = await f.read()
        except OSError as exc:
            log.warning(
                "dm-media-sync: failed to read %s: %s",
                path,
                exc,
            )
            return None
        # Video: pull the first frame as a WebP via VideoProcessor,
        # then re-feed it into the image downscale path so the
        # preview cap (320 px @ Q60) lands the bytes inside the
        # envelope budget. ``VideoProcessor.generate_thumbnail``
        # produces ~512 px @ Q75 — too big for inline shipping.
        if kind == "video":
            try:
                video_frame = await self._video_proc.generate_thumbnail(data)
            except (ValueError, RuntimeError, Exception) as exc:  # pragma: no cover
                # PyAV occasionally raises non-``ValueError`` for
                # codec quirks; treat any extraction failure as
                # "no preview available" — the receiver falls back
                # to the play-glyph placeholder.
                log.debug(
                    "dm-media-sync: video poster extraction failed for %s: %s",
                    media_url,
                    exc,
                )
                return None
            data = video_frame
        try:
            thumb = await self._image_proc.generate_thumbnail(
                data,
                size=PREVIEW_MAX_PX,
            )
        except ValueError:
            # Source isn't a parseable image — happens for HEIC on
            # builds without the codec, or if the upload was
            # somehow a non-image masquerading as one. The receiver
            # falls back to the placeholder until the full blob
            # arrives.
            return None
        return base64.b64encode(thumb).decode("ascii")

    # ── Outbox enqueue ────────────────────────────────────────────────

    async def enqueue_for_message(
        self,
        *,
        message_id: str,
        media_url: str,
        target_instance_ids: list[str],
    ) -> None:
        """Write one ``dm_media_outbox`` row per remote recipient.

        Called by :class:`DmService.send_message` after the
        ``DM_MESSAGE`` envelope has been dispatched. The scheduler
        will pick these up on its next tick and ship the full
        bytes as ``DM_MEDIA_BLOB``.

        ``message_id`` doubles as the ``blob_id`` — every media
        message has exactly one blob, the IDs collapse to one
        identifier the receiver uses to correlate the preview
        envelope with the follow-up bytes. Saves a uuid round-trip
        and one fewer column on the wire.
        """
        path = await self._resolve_media_path(media_url)
        if path is None:
            log.warning(
                "dm-media-sync: cannot enqueue, media_url %s "
                "doesn't resolve under media_dir",
                media_url,
            )
            return
        if not await aiofiles.os.path.isfile(path):
            log.warning(
                "dm-media-sync: cannot enqueue, file missing at %s",
                path,
            )
            return
        for inst in target_instance_ids:
            await self._outbox.enqueue(
                blob_id=message_id,
                message_id=message_id,
                target_instance_id=inst,
                bytes_path=str(path),
            )
        # Nudge the loop so it ships now rather than on the next poll.
        self._wake.set()

    # ── Scheduler loop ────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background flush loop. Idempotent.

        Reclaims any ``in_flight`` rows left orphaned by a previous
        sender crash before the loop's first tick — those rows
        would otherwise stay invisible to :meth:`list_due` and the
        recipient would never get the blob.
        """
        if self._task is not None and not self._task.done():
            return
        try:
            stuck = await self._outbox.reclaim_in_flight()
            if stuck:
                log.info(
                    "dm-media-sync: reclaimed %d stuck in_flight row(s) "
                    "from a previous run",
                    stuck,
                )
        except Exception as exc:  # pragma: no cover
            log.warning("dm-media-sync: reclaim_in_flight failed: %s", exc)
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
            # Clear before flushing so an enqueue that races the flush
            # still leaves ``_wake`` set, and the wait below returns at
            # once instead of losing the signal.
            self._wake.clear()
            try:
                shipped = await self.flush_once()
                if shipped:
                    log.debug(
                        "dm-media-sync: dispatched %d blob(s)",
                        shipped,
                    )
            except Exception as exc:  # pragma: no cover
                log.warning("dm-media-sync flush failed: %s", exc)
            if self._stop.is_set():
                break
            # Wake on a fresh enqueue (``_wake``), else fall back to the
            # periodic poll after ``_interval`` seconds.
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self._interval,
                )
            except asyncio.TimeoutError:
                continue

    async def flush_once(self, *, limit: int = 25) -> int:
        """Run one flush pass — pick due rows and dispatch each.

        Exposed for unit tests + integration drivers (the
        federation-demo harness calls this directly to avoid waiting
        on the periodic tick).
        """
        if self._federation is None:
            # Standalone-mode-style boot without federation wired —
            # nothing to ship; the rows stay pending until the
            # service is properly configured. Same behaviour the
            # general federation outbox has.
            return 0
        fed = self._federation  # non-None past the guard; captured for closures
        due = await self._outbox.list_due(limit=limit)
        shipped = 0
        for entry in due:
            await self._outbox.mark_in_flight(
                blob_id=entry.blob_id,
                target_instance_id=entry.target_instance_id,
            )
            # Per-pair user-visibility gate. Resolve the message's sender so
            # we can ask whether the receiving peer has hidden them.
            try:
                message = await self._convos.get_message(entry.message_id)
            except Exception as exc:  # pragma: no cover — defensive
                log.debug(
                    "dm-media-sync: get_message(%s) failed: %s",
                    entry.message_id,
                    exc,
                )
                message = None
            if message is not None:
                hidden = await self.hidden_for_peer(entry.target_instance_id)
                if message.sender_user_id in hidden:
                    log.debug(
                        "DM_MEDIA_BLOB suppressed: sender %s hidden from %s",
                        message.sender_user_id,
                        entry.target_instance_id,
                    )
                    # Drop the outbox row — the live DM_MESSAGE was already
                    # suppressed by the DmService gate (Task 5) so this blob
                    # has no recipient and should not be retried.
                    await self._outbox.delete(
                        blob_id=entry.blob_id,
                        target_instance_id=entry.target_instance_id,
                    )
                    continue
            try:
                payloads = await self._build_blob_payloads(entry)
            except Exception as exc:
                log.warning(
                    "dm-media-sync: failed to build blob payload for %s → %s: %s",
                    entry.blob_id,
                    entry.target_instance_id,
                    exc,
                )
                await self._reschedule_or_fail(entry, str(exc))
                continue
            # Dispatch chunks with a bounded concurrency window. Order is
            # irrelevant — the receiver writes each by ``chunk_index``
            # idempotently — so pipelining cuts per-chunk round-trip latency
            # (most visibly on the HTTPS fallback). Any chunk failing
            # reschedules the whole row; the next attempt re-sends all chunks
            # and the receiver overwrites the parts it already has.
            # ``send_media_chunk`` ships each chunk over the binary
            # ``fed-media-v1`` channel when the peer supports it, else
            # transparently over the JSON ``DM_MEDIA_BLOB`` path.
            sem = asyncio.Semaphore(PIPELINE_WINDOW)

            async def _send(
                item: tuple[dict, bytes], *, tgt: str = entry.target_instance_id
            ) -> None:
                payload, raw = item
                async with sem:
                    result = await fed.send_media_chunk(
                        to_instance_id=tgt,
                        event_type=FederationEventType.DM_MEDIA_BLOB,
                        payload=payload,
                        raw_chunk=raw,
                    )
                    if not result.ok:
                        raise RuntimeError(result.error or "delivery_failed")

            results = await asyncio.gather(
                *(_send(item) for item in payloads), return_exceptions=True
            )
            send_failed = False
            for (payload, _raw), result in zip(payloads, results):
                if isinstance(result, Exception):
                    log.warning(
                        "dm-media-sync: send failed for %s chunk %d/%d → %s: %s",
                        entry.blob_id,
                        payload.get("chunk_index", 0),
                        payload.get("chunk_count", 1),
                        entry.target_instance_id,
                        result,
                    )
                    send_failed = True
            if send_failed:
                await self._reschedule_or_fail(entry, "chunk send failed")
                continue
            await self._outbox.delete(
                blob_id=entry.blob_id,
                target_instance_id=entry.target_instance_id,
            )
            shipped += 1
        return shipped

    # ── Internals ─────────────────────────────────────────────────────

    async def _build_blob_payloads(self, entry) -> list[tuple[dict, bytes]]:
        """Construct the ``DM_MEDIA_BLOB`` chunk(s) from an outbox row.

        Reads the full file off disk and returns one ``(metadata,
        raw_chunk)`` pair per chunk. The metadata dict carries the
        message + conversation correlation ids and chunk sequencing but
        **not** the bytes — :meth:`FederationService.send_media_chunk`
        ships ``raw_chunk`` as a binary frame on ``fed-media-v1`` (no
        base64) when the peer supports it, or re-attaches it as a base64
        ``bytes_b64`` field on the JSON fallback.

        Files at or below :data:`SINGLE_CHUNK_BYTES_THRESHOLD` ship as
        one chunk with ``chunk_count=1`` (the receiver's single-chunk
        branch). Larger files split into ``ceil(len/MAX_BLOB_CHUNK_BYTES)``
        chunks, each carrying its 0-based ``chunk_index`` + total
        ``chunk_count`` + a ``final`` flag that triggers the receiver's
        concatenate-and-rename finalisation.
        """
        path = pathlib.Path(entry.bytes_path)
        if not await aiofiles.os.path.isfile(path):
            raise FileNotFoundError(f"blob source missing: {path}")
        async with aiofiles.open(path, "rb") as f:
            data = await f.read()
        msg = await self._convos.get_message(entry.message_id)
        if msg is None:
            raise LookupError(f"message {entry.message_id} not found")
        size = len(data)
        common = {
            "media_blob_id": entry.blob_id,
            "message_id": entry.message_id,
            "conversation_id": msg.conversation_id,
            "file_name": msg.file_name,
            "mime_type": msg.mime_type,
            "file_size_bytes": size,
        }
        if size <= SINGLE_CHUNK_BYTES_THRESHOLD:
            return [
                (
                    {**common, "chunk_index": 0, "chunk_count": 1, "final": True},
                    data,
                )
            ]
        chunks: list[tuple[dict, bytes]] = []
        total = (size + MAX_BLOB_CHUNK_BYTES - 1) // MAX_BLOB_CHUNK_BYTES
        for i in range(total):
            start = i * MAX_BLOB_CHUNK_BYTES
            end = min(start + MAX_BLOB_CHUNK_BYTES, size)
            chunks.append(
                (
                    {
                        **common,
                        "chunk_index": i,
                        "chunk_count": total,
                        "final": i == total - 1,
                    },
                    data[start:end],
                )
            )
        return chunks

    async def _reschedule_or_fail(
        self,
        entry,
        last_error: str,
    ) -> None:
        """Bump ``attempts`` + push ``next_attempt_at``; fail at the cap.

        Backoff is exponential with equal jitter
        (:func:`~socialhome.services.backoff.jittered_backoff_seconds`) so
        a swarm of outstanding blobs to the same peer doesn't all hammer
        at the same retry tick, while no single blob can come back due on
        the next tick.
        """
        attempts = entry.attempts + 1
        if attempts >= MAX_ATTEMPTS:
            await self._outbox.mark_failed(
                blob_id=entry.blob_id,
                target_instance_id=entry.target_instance_id,
                last_error=last_error[:500],
            )
            # Mirror the failure onto the message row so the SPA can
            # surface it. Best-effort — if the conversation_messages
            # row has been deleted in the meantime, the update is a
            # no-op.
            await self._convos.update_media_sync_status(
                message_id=entry.message_id,
                status="failed",
            )
            return
        delay = jittered_backoff_seconds(
            attempts=attempts,
            base_seconds=BACKOFF_BASE_SECONDS,
            cap_seconds=BACKOFF_CAP_SECONDS,
        )
        next_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        await self._outbox.reschedule(
            blob_id=entry.blob_id,
            target_instance_id=entry.target_instance_id,
            attempts=attempts,
            next_attempt_at=next_at.strftime("%Y-%m-%d %H:%M:%S"),
            last_error=last_error[:500],
        )

    async def _resolve_media_path(
        self,
        media_url: str | None,
    ) -> pathlib.Path | None:
        """Map a stored ``media_url`` (``api/media/<name>``) to disk.

        Returns ``None`` when ``media_url`` doesn't fit the expected
        shape — e.g. an external URL, a malformed value, or a path
        that tries to escape the media root. We never read outside
        ``self._media_dir`` even if the stored URL is malicious.
        """
        if not media_url:
            return None
        # The stored canonical URL is ``api/media/<filename>``; the
        # SPA's optimistic-send path may also leave a signed
        # variant with ``?exp=&sig=`` query string already stripped
        # by ``strip_signature_query`` before reaching us.
        url = media_url.strip("/").lstrip()
        prefix = "api/media/"
        if not url.startswith(prefix):
            return None
        name = url[len(prefix) :]
        # No traversal. ``Path.resolve()`` is a sync syscall (stat
        # walk) — wrap in ``asyncio.to_thread`` so this method stays
        # await-safe even when the media dir lives on a slow disk.
        candidate = await asyncio.to_thread((self._media_dir / name).resolve)
        try:
            media_root = await asyncio.to_thread(self._media_dir.resolve)
            candidate.relative_to(media_root)
        except ValueError:
            return None
        return candidate
