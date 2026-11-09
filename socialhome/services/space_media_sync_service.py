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
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

import aiofiles
import aiofiles.os

from ..domain.federation import FederationEventType
from ..domain.file_hashes import blob_hash_fields, sha256_hex
from ..domain.media_chunking import split_into_chunks
from ..repositories.space_media_outbox_repo import (
    AbstractSpaceMediaOutboxRepo,
    SpaceMediaOutboxEntry,
)
from .backoff import jittered_backoff_seconds
from .media_chunk_pipeline import send_chunks
from .media_file_hasher import MediaFileHasher
from .peer_unpair_service import is_unpair_tombstone

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo

log = logging.getLogger(__name__)

#: One row's send (every chunk of one file to one household) gets at most
#: this long: rows go out one at a time, so a send that hangs (a mesh path
#: that never answers) must not hold every later recipient back. A row
#: that runs out is rescheduled like any failed one.
ROW_SEND_TIMEOUT_S: float = 300.0
#: Recipients served at once by one flush pass.
PARALLEL_RECIPIENTS: int = 6


class _RelayReaders(Protocol):
    async def is_relay_reader(self, instance_id: str) -> bool: ...


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
        "_federation",
        "_federation_repo",
        "_interval",
        "_media_dir",
        "_outbox",
        "_relay_readers",
        "_stop",
        "_task",
        "_wake",
        "file_hasher",
    )

    def __init__(
        self,
        *,
        outbox: AbstractSpaceMediaOutboxRepo,
        federation: FederationService | None,
        media_dir: pathlib.Path,
        interval_seconds: float = 5.0,
        federation_repo: AbstractFederationRepo | None = None,
    ) -> None:
        self._outbox = outbox
        self._federation = federation
        #: Only to recognise an unpair tombstone (see :func:`is_unpair_tombstone`).
        self._federation_repo = federation_repo
        self._media_dir = media_dir
        #: v_85 — the household's one file hasher (shared by every
        #: sender of a space item, so a file described then shipped is
        #: hashed once).
        self.file_hasher = MediaFileHasher(media_dir)
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Set by ``enqueue_for_blob`` so freshly-queued media ships
        # immediately instead of waiting up to ``interval_seconds`` for
        # the next poll. ``_interval`` remains a fallback poll.
        self._wake = asyncio.Event()
        #: v_80 — who reads the space's pictures from a GFS media cache
        #: instead (:meth:`SpaceMediaGfsUploader.is_relay_reader`).
        self._relay_readers: _RelayReaders | None = None

    def attach_relay_readers(self, readers: _RelayReaders) -> None:
        """Wire the v_80 reader check: a member household seated from an
        invite link at v_80 gets no ``SPACE_MEDIA_BLOB`` push — the GFS
        relay refuses those bytes, and it fetches the file from its GFS's
        media cache instead."""
        self._relay_readers = readers

    def attach_federation(self, federation: FederationService) -> None:
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
            log.warning(
                "space-media-sync: reclaim_in_flight failed: %s", exc, exc_info=True
            )
        try:
            # Backstop for a crash mid-teardown: rows for a household that is
            # gone could only ever be refused.
            orphaned = await self._outbox.purge_orphaned()
            if orphaned:
                log.info(
                    "space-media-sync: dropped %d row(s) for households that are gone",
                    orphaned,
                )
        except Exception as exc:  # pragma: no cover
            log.warning(
                "space-media-sync: purge_orphaned failed: %s", exc, exc_info=True
            )
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
                log.warning("space-media-sync flush failed: %s", exc, exc_info=True)
            if self._stop.is_set():
                break
            # Wake on a fresh enqueue, else fall back to the periodic poll.
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self._interval,
                )
            except TimeoutError:
                continue

    async def flush_once(self, *, limit: int = 25) -> int:
        """Run one flush pass — pick due rows and dispatch each.

        Returns the number of rows that shipped successfully.
        """
        if self._federation is None:
            return 0
        fed = self._federation  # non-None past the guard; captured for closures
        due = await self._outbox.list_due(limit=limit)
        # Like the GFS: one copy of the bytes, one row per recipient, and the
        # recipients served in parallel (at most :data:`PARALLEL_RECIPIENTS`
        # at once, each recipient's rows one after the other), so a slow or
        # stuck recipient never holds the others back.
        by_target: dict[str, list[SpaceMediaOutboxEntry]] = {}
        for entry in due:
            by_target.setdefault(entry.target_instance_id, []).append(entry)
        gate = asyncio.Semaphore(PARALLEL_RECIPIENTS)

        async def _serve(rows: list[SpaceMediaOutboxEntry]) -> int:
            n = 0
            async with gate:
                for row in rows:
                    if await self._dispatch(row, fed):
                        n += 1
            return n

        results = await asyncio.gather(
            *(_serve(rows) for rows in by_target.values()), return_exceptions=True
        )
        shipped = 0
        for result in results:
            if isinstance(result, BaseException):
                log.warning(
                    "space-media-sync: a recipient's sends failed",
                    exc_info=result,
                )
            else:
                shipped += result
        return shipped

    async def _dispatch(
        self, entry: SpaceMediaOutboxEntry, fed: FederationService
    ) -> bool:
        """Send one row (every chunk of one file to one household); whether
        it shipped."""
        if await is_unpair_tombstone(self._federation_repo, entry.target_instance_id):
            # Not even over the mesh fallback: it gets our UNPAIR and
            # nothing else (unpairing already purged the rest).
            await self._outbox.delete(
                blob_id=entry.blob_id,
                target_instance_id=entry.target_instance_id,
            )
            return False
        if self._relay_readers is not None and (
            await self._relay_readers.is_relay_reader(entry.target_instance_id)
        ):
            # Decided at send time, not when the row was queued: a view
            # that was stale then (a GFS just restarted without its cache)
            # must not drop the push for good. A reader takes the file from
            # its GFS's media cache instead.
            log.debug(
                "space-media-sync: %s reads %s from its GFS — no push",
                entry.target_instance_id,
                entry.blob_id,
            )
            await self._outbox.delete(
                blob_id=entry.blob_id,
                target_instance_id=entry.target_instance_id,
            )
            return False
        await self._outbox.mark_in_flight(
            blob_id=entry.blob_id,
            target_instance_id=entry.target_instance_id,
        )
        log.debug(
            "space-media-sync: sending %s → %s",
            entry.blob_id,
            entry.target_instance_id,
        )
        started = time.monotonic()
        try:
            payloads = await self._build_blob_payloads(entry, fed)
        except Exception as exc:
            log.warning(
                "space-media-sync: build failed for %s → %s: %s",
                entry.blob_id,
                entry.target_instance_id,
                exc,
                exc_info=True,
            )
            await self._reschedule_or_fail(entry, str(exc))
            return False
        # Use mesh fallback so a member household that isn't a
        # CONFIRMED direct peer (joined via §D1b on a mesh path
        # like c↔b↔d) still receives the bytes — the chunks
        # ride SPACE_ROUTED end-to-end-sealed through the
        # relays. A direct-peer path uses ``send_event``
        # internally so the per-chunk overhead matches the DM
        # case for the common LAN-paired scenario.
        # Dispatch chunks with a bounded concurrency window (order is
        # irrelevant — the receiver writes each by ``chunk_index``):
        # ``PIPELINE_WINDOW`` workers pull chunks lazily, so a file in
        # many chunks never becomes a coroutine per chunk.
        # ``send_with_mesh_fallback`` doesn't raise on delivery failure;
        # it returns ``DeliveryResult(ok=False)`` (no_route /
        # not_confirmed / transport blip), so we raise on that to funnel
        # both failure modes into one. Any chunk failing reschedules the
        # whole row.

        async def _send(
            payload: dict,
            raw: bytes,
            *,
            tgt: str = entry.target_instance_id,
            space_id: str = entry.space_id,
        ) -> None:
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
                # The envelope's routing ``space_id`` — every other
                # space fan-out sets it, and these frames did not:
                # the §24.11 ban check reads it off the envelope and
                # silently no-ops when it is absent, so a household
                # banned from the space could still push bytes at us.
                space_id=space_id,
            )
            if not result.ok:
                raise RuntimeError(result.error or "delivery_failed")

        try:
            failure = await asyncio.wait_for(
                send_chunks(payloads, _send, window=PIPELINE_WINDOW),
                timeout=ROW_SEND_TIMEOUT_S,
            )
        except TimeoutError:
            failure = ({}, TimeoutError(f"timeout after {ROW_SEND_TIMEOUT_S:.0f} s"))
        log.debug(
            "space-media-sync: %s → %s %s after %.1f s",
            entry.blob_id,
            entry.target_instance_id,
            "failed" if failure is not None else "sent",
            time.monotonic() - started,
        )
        if failure is not None:
            payload, error = failure
            log.warning(
                "space-media-sync: chunk %d/%d failed for %s → %s: %s",
                payload.get("chunk_index", 0),
                payload.get("chunk_count", 1),
                entry.blob_id,
                entry.target_instance_id,
                error,
            )
            await self._reschedule_or_fail(entry, str(error) or "chunk send failed")
            return False
        await self._outbox.delete(
            blob_id=entry.blob_id,
            target_instance_id=entry.target_instance_id,
        )
        return True

    # ── Internals ─────────────────────────────────────────────────────

    async def _build_blob_payloads(
        self,
        entry: SpaceMediaOutboxEntry,
        fed: FederationService,
    ) -> Iterator[tuple[dict, bytes]]:
        """Build ``SPACE_MEDIA_BLOB`` chunk(s) from a row.

        Returns one ``(metadata, raw_chunk)`` pair per chunk. The
        metadata carries the correlation ids + chunk sequencing but not
        the bytes — :meth:`FederationService.send_media_chunk` ships
        ``raw_chunk`` as a binary frame on ``fed-media-v1`` to a CONFIRMED
        v_14+ peer, else re-attaches it as base64 ``bytes_b64`` on the
        JSON / ``SPACE_ROUTED`` fallback.

        Chunks sized for the target household and this file
        (:meth:`FederationService.mesh_media_chunk_bytes`): one chunk when
        the file fits; smaller for a member reached only over the mesh,
        whose chunks travel sealed inside ``SPACE_ROUTED``. Each carries ``chunk_index`` + ``chunk_count`` +
        ``final`` so the receiver can assemble. Lazy — each chunk is
        built when the sender pulls it.
        """
        path = pathlib.Path(entry.bytes_path)
        if not await aiofiles.os.path.isfile(path):
            raise FileNotFoundError(f"blob source missing: {path}")
        async with aiofiles.open(path, "rb") as f:
            data = await f.read()
        size = len(data)
        chunk_bytes = await fed.mesh_media_chunk_bytes(
            entry.target_instance_id, file_size_bytes=size
        )
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
            # v_85 — the whole file's hash: the receiver stores a hashed
            # file content-addressed and checks the assembled bytes first.
            **blob_hash_fields(await self._file_hash(entry.blob_id, data)),
        }
        return (
            ({**common, **seq}, raw)
            for seq, raw in split_into_chunks(data, chunk_bytes)
        )

    async def _file_hash(self, name: str, data: bytes) -> str:
        """The SHA-256 of *data*, read from the household hasher's cache
        when it holds this very file (the same size) — a file shipped to
        many households, or described then shipped, is hashed once."""
        cached = await self.file_hasher.hash_name(name)
        if cached is not None and cached.size == len(data):
            return cached.h
        return await asyncio.to_thread(sha256_hex, data)

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
        next_at = datetime.now(UTC) + timedelta(seconds=delay)
        await self._outbox.reschedule(
            blob_id=entry.blob_id,
            target_instance_id=entry.target_instance_id,
            attempts=attempts,
            next_attempt_at=next_at.strftime("%Y-%m-%d %H:%M:%S"),
            last_error=last_error[:500],
        )


# uuid is imported for future use (transfer IDs that aren't filename-derived).
_ = uuid
