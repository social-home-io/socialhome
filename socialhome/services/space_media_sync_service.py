"""Federation media-blob sync for space posts.

Mirrors :class:`DmMediaSyncService` for the space case: when
``SpacePostOutbound`` broadcasts ``SPACE_POST_CREATED`` to peer
households, the post payload carries only the URL strings — the
bytes only live on the sender's media path. This service ships the
actual bytes via ``SPACE_MEDIA_BLOB`` events, one per peer per
referenced media file, with chunking for large videos.

Lifecycle:

* ``enqueue_for_post`` — called by :class:`SpacePostOutbound` after a
  successful ``SPACE_POST_CREATED`` broadcast. Inserts one row per
  ``(blob_id, target_instance_id)`` tuple. Idempotent.
* ``start`` / ``stop`` — :class:`asyncio.Event`-driven background loop
  that polls the outbox table.
* ``flush_once`` — one tick: pick due rows, build chunks, send.
  Exposed for tests + the federation-demo harness so callers can
  drive a flush without waiting on the periodic tick.

The DM equivalent and this service are deliberately separate (rather
than a shared MediaSyncService) so the schedulers can backoff
independently. A stuck DM peer doesn't starve space sends and
vice-versa.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import aiofiles
import aiofiles.os

from ..domain.federation import FederationEventType
from ..repositories.space_media_outbox_repo import (
    AbstractSpaceMediaOutboxRepo,
    SpaceMediaOutboxEntry,
)
from .backoff import jittered_backoff_seconds

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService

log = logging.getLogger(__name__)


#: Files ≤ this threshold ship as one chunk; above this they're
#: split into :data:`MAX_BLOB_CHUNK_BYTES` chunks. Same value as
#: :data:`socialhome.services.dm_media_sync_service.SINGLE_CHUNK_BYTES_THRESHOLD`
#: — keeps the wire shape comparable.
SINGLE_CHUNK_BYTES_THRESHOLD: int = 1024 * 1024
#: 512 KiB chunks keep the federation envelope under the ~1 MiB
#: per-event send budget (~700 KB after sig + base64 expansion) while
#: halving the chunk count vs the old 256 KiB. Matches
#: :data:`socialhome.services.dm_media_sync_service.MAX_BLOB_CHUNK_BYTES`.
MAX_BLOB_CHUNK_BYTES: int = 512 * 1024
#: Max chunks of one blob in flight at once (see DmMediaSyncService). Small
#: so the RTC ~1 MiB send buffer can't be overrun — it just backpressures.
PIPELINE_WINDOW: int = 4
#: Cap on retry attempts before a row is moved to ``status='failed'``.
#: Same as the DM curve so behaviour matches.
MAX_ATTEMPTS: int = 6
BACKOFF_BASE_SECONDS: float = 30.0
BACKOFF_CAP_SECONDS: float = 30 * 60.0


class SpaceMediaSyncService:
    """Build + dispatch ``SPACE_MEDIA_BLOB`` rows."""

    __slots__ = (
        "_outbox",
        "_federation",
        "_media_dir",
        "_interval",
        "_task",
        "_stop",
        "_wake",
    )

    def __init__(
        self,
        *,
        outbox: AbstractSpaceMediaOutboxRepo,
        federation: "FederationService | None",
        media_dir: pathlib.Path,
        interval_seconds: float = 5.0,
    ) -> None:
        self._outbox = outbox
        self._federation = federation
        self._media_dir = media_dir
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Set by ``enqueue_for_blob`` so freshly-queued media ships
        # immediately instead of waiting up to ``interval_seconds`` for
        # the next poll. ``_interval`` remains a fallback poll.
        self._wake = asyncio.Event()

    def attach_federation(self, federation: "FederationService") -> None:
        """Wire federation after construction (breaks the boot cycle).

        :class:`FederationService` is built later than this service
        in :func:`create_app`. Stash the handle here once it exists.
        """
        self._federation = federation

    # ── Enqueue ──────────────────────────────────────────────────────────

    async def enqueue_for_blob(
        self,
        *,
        space_id: str,
        correlation_id: str,
        target_instance_ids: list[str],
        media_urls: list[str],
    ) -> None:
        """One outbox row per (media_url, peer) tuple.

        Generic enqueue surface — :class:`SpacePostOutbound` and
        :class:`GalleryFederationOutbound` both call this. The
        ``correlation_id`` is a soft backref (post_id, gallery item
        id, etc.) for debug + post-delete cleanup; the scheduler
        itself never reads it.

        ``blob_id`` is derived from the filename so the same file
        referenced by multiple posts / items dedups at the
        ``(blob_id, target_instance_id)`` primary key — a re-shared
        image doesn't ship twice.
        """
        for url in media_urls:
            filename = url.rsplit("/", 1)[-1].split("?", 1)[0]
            if not filename:
                continue
            path = self._media_dir / filename
            blob_id = filename
            for target in target_instance_ids:
                if not target:
                    continue
                await self._outbox.enqueue(
                    blob_id=blob_id,
                    space_id=space_id,
                    correlation_id=correlation_id,
                    target_instance_id=target,
                    bytes_path=str(path),
                )
        # Nudge the loop so it ships now rather than on the next poll.
        self._wake.set()

    # Back-compat alias for the post-only signature shipped in PR #440.
    # ``space_id`` defaults to ``""`` for callers that don't have it
    # to hand — the FK isn't enforced for non-existent space rows on
    # enqueue, only on delete-cascade.
    async def enqueue_for_post(
        self,
        *,
        post_id: str,
        target_instance_ids: list[str],
        media_urls: list[str],
        space_id: str = "",
    ) -> None:
        await self.enqueue_for_blob(
            space_id=space_id,
            correlation_id=post_id,
            target_instance_ids=target_instance_ids,
            media_urls=media_urls,
        )

    # ── Scheduler loop ────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background flush loop. Idempotent.

        Reclaims any ``in_flight`` rows left orphaned by a previous
        sender crash before the loop's first tick.
        """
        if self._task is not None and not self._task.done():
            return
        try:
            stuck = await self._outbox.reclaim_in_flight()
            if stuck:
                log.info(
                    "space-media-sync: reclaimed %d stuck in_flight row(s)",
                    stuck,
                )
        except Exception as exc:  # pragma: no cover
            log.warning("space-media-sync: reclaim_in_flight failed: %s", exc)
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
            # Clear before flushing so an enqueue racing the flush still
            # leaves ``_wake`` set and the wait below returns at once.
            self._wake.clear()
            try:
                shipped = await self.flush_once()
                if shipped:
                    log.debug(
                        "space-media-sync: dispatched %d blob(s)",
                        shipped,
                    )
            except Exception as exc:  # pragma: no cover
                log.warning("space-media-sync flush failed: %s", exc)
            if self._stop.is_set():
                break
            # Wake on a fresh enqueue, else fall back to the periodic poll.
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self._interval,
                )
            except asyncio.TimeoutError:
                continue

    async def flush_once(self, *, limit: int = 25) -> int:
        """Run one flush pass — pick due rows and dispatch each.

        Returns the number of rows that shipped successfully.
        """
        if self._federation is None:
            return 0
        fed = self._federation  # non-None past the guard; captured for closures
        due = await self._outbox.list_due(limit=limit)
        shipped = 0
        for entry in due:
            await self._outbox.mark_in_flight(
                blob_id=entry.blob_id,
                target_instance_id=entry.target_instance_id,
            )
            try:
                payloads = await self._build_blob_payloads(entry)
            except Exception as exc:
                log.warning(
                    "space-media-sync: build failed for %s → %s: %s",
                    entry.blob_id,
                    entry.target_instance_id,
                    exc,
                )
                await self._reschedule_or_fail(entry, str(exc))
                continue
            # Use mesh fallback so a member household that isn't a
            # CONFIRMED direct peer (joined via §D1b on a mesh path
            # like c↔b↔d) still receives the bytes — the chunks
            # ride SPACE_ROUTED end-to-end-sealed through the
            # relays. A direct-peer path uses ``send_event``
            # internally so the per-chunk overhead matches the DM
            # case for the common LAN-paired scenario.
            # Dispatch chunks with a bounded concurrency window (order is
            # irrelevant — the receiver writes each by ``chunk_index``).
            # ``send_with_mesh_fallback`` doesn't raise on delivery failure;
            # it returns ``DeliveryResult(ok=False)`` (no_route /
            # not_confirmed / transport blip), so we raise on that inside the
            # task to funnel both failure modes through ``gather``. Any chunk
            # failing reschedules the whole row.
            sem = asyncio.Semaphore(PIPELINE_WINDOW)

            async def _send(
                item: tuple[dict, bytes], *, tgt: str = entry.target_instance_id
            ) -> None:
                payload, raw = item
                async with sem:
                    # ``mesh_fallback=True`` keeps the existing semantics:
                    # a CONFIRMED v_14+ peer gets the binary channel; a
                    # non-CONFIRMED / mesh-only member still receives the
                    # bytes as base64 over SPACE_ROUTED. The binary channel
                    # is point-to-point only, so mesh members never use it.
                    result = await fed.send_media_chunk(
                        to_instance_id=tgt,
                        event_type=FederationEventType.SPACE_MEDIA_BLOB,
                        payload=payload,
                        raw_chunk=raw,
                        mesh_fallback=True,
                    )
                    if not result.ok:
                        raise RuntimeError(result.error or "delivery_failed")

            results = await asyncio.gather(
                *(_send(item) for item in payloads), return_exceptions=True
            )
            send_failed = False
            first_error = ""
            for (payload, _raw), result in zip(payloads, results):
                if isinstance(result, Exception):
                    log.warning(
                        "space-media-sync: chunk %d/%d failed for %s → %s: %s",
                        payload.get("chunk_index", 0),
                        payload.get("chunk_count", 1),
                        entry.blob_id,
                        entry.target_instance_id,
                        result,
                    )
                    send_failed = True
                    first_error = first_error or str(result)
            if send_failed:
                await self._reschedule_or_fail(
                    entry, first_error or "chunk send failed"
                )
                continue
            await self._outbox.delete(
                blob_id=entry.blob_id,
                target_instance_id=entry.target_instance_id,
            )
            shipped += 1
        return shipped

    # ── Internals ─────────────────────────────────────────────────────

    async def _build_blob_payloads(
        self,
        entry: SpaceMediaOutboxEntry,
    ) -> list[tuple[dict, bytes]]:
        """Build ``SPACE_MEDIA_BLOB`` chunk(s) from a row.

        Returns one ``(metadata, raw_chunk)`` pair per chunk. The
        metadata carries the correlation ids + chunk sequencing but not
        the bytes — :meth:`FederationService.send_media_chunk` ships
        ``raw_chunk`` as a binary frame on ``fed-media-v1`` to a CONFIRMED
        v_14+ peer, else re-attaches it as base64 ``bytes_b64`` on the
        JSON / ``SPACE_ROUTED`` fallback.

        Single-chunk for files ≤ :data:`SINGLE_CHUNK_BYTES_THRESHOLD`;
        multi-chunk otherwise. Each chunk carries ``chunk_index`` +
        ``chunk_count`` + ``final`` so the receiver can assemble.
        """
        path = pathlib.Path(entry.bytes_path)
        if not await aiofiles.os.path.isfile(path):
            raise FileNotFoundError(f"blob source missing: {path}")
        async with aiofiles.open(path, "rb") as f:
            data = await f.read()
        size = len(data)
        # Stable transfer id per (blob, peer) so retries assemble into
        # the SAME part files on the receiver rather than starting fresh.
        transfer_id = f"{entry.blob_id}:{entry.target_instance_id}"
        common: dict = {
            # ``post_id`` kept for back-compat with v1 inbound; new
            # receivers should consult ``correlation_id`` which
            # carries either a post_id or a gallery_item_id.
            "post_id": entry.correlation_id,
            "correlation_id": entry.correlation_id,
            "space_id": entry.space_id,
            "blob_id": entry.blob_id,
            "transfer_id": transfer_id,
            "filename": entry.blob_id,
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
        entry: SpaceMediaOutboxEntry,
        last_error: str,
    ) -> None:
        """Exponential backoff with equal jitter; ``status='failed'`` at cap."""
        attempts = entry.attempts + 1
        if attempts >= MAX_ATTEMPTS:
            await self._outbox.mark_failed(
                blob_id=entry.blob_id,
                target_instance_id=entry.target_instance_id,
                last_error=last_error[:500],
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


# uuid is imported for future use (transfer IDs that aren't filename-derived).
_ = uuid
