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
from collections import OrderedDict
from collections.abc import Iterator, Mapping, Sequence
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
from .inbound_media_store import is_safe_media_name, media_basename, media_file_path
from .media_chunk_pipeline import send_chunks
from .media_file_hasher import MediaFileHasher
from .peer_unpair_service import is_unpair_tombstone

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo

log = logging.getLogger(__name__)

#: One chunk's send gets at most this long (no progress for so long): a
#: send that hangs (a mesh path that never answers) fails its row, which
#: is rescheduled like any failed one. Per chunk, not per file, so a big
#: file over a slow path that keeps moving is still delivered.
CHUNK_IDLE_TIMEOUT_S: float = 120.0
#: The files one flush pass holds in memory at once (recipients go in
#: parallel); a file larger than this goes alone.
MAX_INFLIGHT_FILE_BYTES: int = 64 * 1024 * 1024
#: Recipients served at once by one flush pass.
PARALLEL_RECIPIENTS: int = 6
#: ``(file, household)`` rows remembered from the start of their send, oldest dropped,
#: so a chunk the mesh reports lost (:meth:`SpaceMediaSyncService
#: .on_mesh_chunk_lost`) can queue the file again. In memory only: a nack
#: arrives within the mesh's pending window (minutes) or never.
MAX_SENT_MARKS: int = 512
#: v_92 — ``(file, household)`` rows queued because the household asked for
#: the file (``SPACE_MEDIA_WANTED``) remembered at once, oldest dropped. In
#: memory only: a mark lost to a restart costs one shortcut-dropped push,
#: and the requester asks again on its next view.
MAX_PUSHED_MARKS: int = 4096


#: Where a sent row's one mesh resend stands (``SpaceMediaSyncService._sent``).
_SENT, _RESEND_QUEUED, _RESENT = range(3)


class _RelayReaders(Protocol):
    async def reads_from_gfs(self, instance_id: str, filename: str) -> bool: ...


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
        "_budget",
        "_federation",
        "_federation_repo",
        "_held",
        "_interval",
        "_media_dir",
        "_outbox",
        "_pushed",
        "_relay_readers",
        "_sent",
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
        #: instead (:meth:`SpaceMediaGfsUploader.reads_from_gfs`).
        self._relay_readers: _RelayReaders | None = None
        #: Bytes of the files the current pass holds (``MAX_INFLIGHT_FILE_BYTES``).
        self._budget = asyncio.Condition()
        self._held = 0
        #: v_92 — rows a household asked for (:data:`MAX_PUSHED_MARKS`).
        self._pushed: OrderedDict[tuple[str, str], None] = OrderedDict()
        #: ``(file, household)`` → the row as it was sent and where its one
        #: resend stands: ``_SENT`` → (a chunk is reported lost) →
        #: ``_RESEND_QUEUED`` → (the row is sent again) → ``_RESENT``
        #: (:data:`MAX_SENT_MARKS`).
        self._sent: OrderedDict[tuple[str, str], tuple[SpaceMediaOutboxEntry, int]] = (
            OrderedDict()
        )

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
        push: bool = False,
        wire_names: Mapping[str, str] | None = None,
    ) -> None:
        """One outbox row per (media_url, peer) tuple.

        ``push`` (v_92): the targets asked for these files
        (``SPACE_MEDIA_WANTED``) — they already missed their GFS cache, so
        the send-time "reads from its GFS" shortcut never drops these rows.

        ``wire_names`` (v_92): local file name → the name it is SENT under
        (default: its local name). A provider ships an asked-for file under
        the requested content name (``sc-``) even when it stores it under
        its own reader name (``sg-``), which a receiver refuses by name. An
        unsafe wire name queues nothing.

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
            blob_id = (wire_names or {}).get(filename, filename)
            if not is_safe_media_name(blob_id):
                log.warning(
                    "space-media-sync: unsafe wire name %r for %s — not queued",
                    blob_id,
                    filename,
                )
                continue
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
                if push:
                    key = (blob_id, target)
                    self._pushed[key] = None
                    self._pushed.move_to_end(key)
                    while len(self._pushed) > MAX_PUSHED_MARKS:
                        self._pushed.popitem(last=False)
        # Nudge the loop so it ships now rather than on the next poll.
        self._wake.set()

    async def held_files(self, media_urls: Sequence[str]) -> list[str]:
        """The *media_urls* whose file is in this household's media dir, in
        order. A catch-up queues only these: a row can be here without its
        file (it never arrived), and a file that is not here cannot ship."""
        held: list[str] = []
        for url in media_urls:
            path = media_file_path(self._media_dir, media_basename(url))
            if path is not None and await aiofiles.os.path.isfile(path):
                held.append(url)
        return held

    async def on_mesh_chunk_lost(self, target_instance_id: str, head: dict) -> None:
        """A chunk of ``head["blob_id"]`` we sent to *target_instance_id*
        over the mesh came back nacked (``SPACE_ROUTE_STALE``: the target
        restarted and cannot open what was sealed to its old key).

        The relay had taken the chunk, so the row was done and deleted (or
        is still being sent: its later chunks — then ``_send_row`` keeps the
        row instead of deleting it); the mesh keeps no copy of a chunk to
        retransmit. Queue the whole file again — once: the other chunks' nacks find the row already queued,
        and a nack for the resend gives up (the receiver asks for the file
        when someone opens it, v_92). Never raises.
        """
        blob_id = head.get("blob_id")
        if not isinstance(blob_id, str):
            return
        key = (blob_id, target_instance_id)
        sent = self._sent.get(key)
        if sent is None:
            return
        entry, state = sent
        if state != _SENT:
            # Another chunk of the first send (the file is queued already),
            # or the resend itself: nothing more to do.
            log.debug(
                "space-media-sync: %s → %s lost on the mesh — %s",
                blob_id,
                target_instance_id,
                "already queued again"
                if state == _RESEND_QUEUED
                else "the resend too; not sent a third time",
            )
            return
        try:
            await self._outbox.enqueue(
                blob_id=entry.blob_id,
                space_id=entry.space_id,
                correlation_id=entry.correlation_id,
                target_instance_id=entry.target_instance_id,
                bytes_path=entry.bytes_path,
            )
        except Exception:
            log.warning(
                "space-media-sync: queueing %s → %s again failed",
                blob_id,
                target_instance_id,
                exc_info=True,
            )
            return
        log.info(
            "space-media-sync: %s → %s was lost on the mesh (the target "
            "could not open it) — queued again",
            blob_id,
            target_instance_id,
        )
        self._sent[key] = (entry, _RESEND_QUEUED)
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
        # A row a send left ``in_flight`` when it died past ``Exception``
        # (an inner cancellation, a DB error while rescheduling) is taken
        # back on every pass, not only at the next start. ``_loop`` is the
        # only caller, so passes never overlap.
        await self._outbox.reclaim_in_flight()
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
        key = (entry.blob_id, entry.target_instance_id)
        if (
            self._relay_readers is not None
            and key not in self._pushed
            and await self._relay_readers.reads_from_gfs(
                entry.target_instance_id, entry.blob_id
            )
        ):
            # Decided at send time, not when the row was queued, and only
            # when the reader's GFS is known to hold the file (or the
            # reader can ask for it): a GFS whose cache was off when the
            # file was planned never got it, so that reader keeps the push.
            # A file the reader asked for (v_92, ``push``) is never dropped:
            # it already missed its GFS.
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
        try:
            size = (await aiofiles.os.stat(entry.bytes_path)).st_size
        except OSError:
            size = 0  # the build below reports the missing file
        need = min(size, MAX_INFLIGHT_FILE_BYTES)
        async with self._budget:
            await self._budget.wait_for(
                lambda: self._held + need <= MAX_INFLIGHT_FILE_BYTES
            )
            self._held += need
        try:
            sent = await self._send_row(entry, fed)
            if sent:
                self._pushed.pop(key, None)
            return sent
        finally:
            async with self._budget:
                self._held -= need
                self._budget.notify_all()

    async def _send_row(
        self, entry: SpaceMediaOutboxEntry, fed: FederationService
    ) -> bool:
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
            try:
                result = await asyncio.wait_for(
                    fed.send_media_chunk(
                        to_instance_id=tgt,
                        event_type=FederationEventType.SPACE_MEDIA_BLOB,
                        payload=payload,
                        raw_chunk=raw,
                        mesh_fallback=True,
                        # The envelope's routing ``space_id`` — every other
                        # space fan-out sets it, and these frames did not:
                        # the §24.11 ban check reads it off the envelope
                        # and silently no-ops when it is absent, so a
                        # household banned from the space could still push
                        # bytes at us.
                        space_id=space_id,
                    ),
                    timeout=CHUNK_IDLE_TIMEOUT_S,
                )
            except TimeoutError:
                raise RuntimeError(
                    f"timeout: no progress for {CHUNK_IDLE_TIMEOUT_S:.0f} s"
                ) from None
            if not result.ok:
                raise RuntimeError(result.error or "delivery_failed")

        # Marked before the first chunk goes: a nack can arrive while later
        # chunks are still being sent (the target is back, the route cached).
        key = (entry.blob_id, entry.target_instance_id)
        before = self._sent.get(key)
        self._remember_sent(entry)
        failure = await send_chunks(payloads, _send, window=PIPELINE_WINDOW)
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
            if before is not None and before[1] == _RESEND_QUEUED:
                # The resend itself failed: its retry is still the resend.
                self._sent[key] = before
            await self._reschedule_or_fail(entry, str(error) or "chunk send failed")
            return False
        mark = self._sent.get(key)
        if mark is not None and mark[1] == _RESEND_QUEUED:
            # A chunk was reported lost during this very send: the row is
            # the resend — due now, not deleted.
            await self._outbox.reschedule(
                blob_id=entry.blob_id,
                target_instance_id=entry.target_instance_id,
                attempts=entry.attempts,
                next_attempt_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                last_error="a chunk was lost on the mesh",
            )
            self._wake.set()
            return True
        await self._outbox.delete(
            blob_id=entry.blob_id,
            target_instance_id=entry.target_instance_id,
        )
        return True

    def _remember_sent(self, entry: SpaceMediaOutboxEntry) -> None:
        """Keep the row being sent for :meth:`on_mesh_chunk_lost`. A row
        queued again by it stays marked as the resend."""
        key = (entry.blob_id, entry.target_instance_id)
        previous = self._sent.pop(key, None)
        resend = previous is not None and previous[1] == _RESEND_QUEUED
        self._sent[key] = (entry, _RESENT if resend else _SENT)
        while len(self._sent) > MAX_SENT_MARKS:
            self._sent.popitem(last=False)

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
