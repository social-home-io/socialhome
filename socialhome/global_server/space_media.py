"""The GFS space media cache — encrypted, short-lived, bounded.

Households reached only through a GFS (a space member seated from an invite
link, a follower of a public / global space) can't fetch a picture from the
household that posted it. So a household encrypts each space file ONCE —
under the space content key, padded to a size bucket — and uploads the same
ciphertext ONCE per GFS; every reader fetches it here by its SHA-256. This
server can't open it.

**Scopes and claims.** A blob is held by one or more **claims**, each by a
scope — a listed space (``space`` + its id) or a private space's opaque v_51
channel (``channel`` + the random channel id; this server never learns which
space that is). The bytes are stored once: identical bytes uploaded for
another scope add that scope's claim (a coalesced ``False``), never a copy,
and never a conflict — so nobody who saw a public blob can squat its address
under their own scope and delete it from under the real one. A delete drops
only the caller's claim; the blob and its file go with the last claim. A
blob expires with its latest claim.

It is a **cache, not storage** — the operator sizes it to the disk they have
(``[space_media]`` in the GFS config, :class:`SpaceMediaLimits`), and it
favours new pictures over old ones:

* a claim lives at most ``ttl_seconds`` (3 days by default), whatever expiry
  the uploader asks; a re-upload by a scope that already holds the blob does
  NOT extend it. The maintenance sweep (:meth:`GfsSpaceMediaStore.sweep`)
  drops expired blobs, claims and upload counts, and files with no row;
* one scope's live claims hold at most ``space_max_bytes`` (256 MiB): when a
  new one would pass it, that scope's own claims on its **least recently
  fetched** (then oldest) blobs are evicted until it fits — a blob another
  scope still claims stays;
* all blobs together stay under ``max_bytes`` (5 GiB): before a new blob is
  stored, expired blobs go first, then the least recently fetched (then
  oldest) of any scope, with all their claims — just enough to fit; the
  backlog of expired blobs is the sweep's;
* a blob larger than either budget is ``413``; a scope accepting more than
  ``space_uploads_per_hour`` uploads (a sliding hour, counted in
  ``gfs_space_media_uploads`` — eviction and delete can't reset it) is
  ``429 space_media_quota``; a cache the operator turned off is ``503
  space_media_disabled``.

A fetch marks the blob at most once an hour (:data:`FETCH_TOUCH_INTERVAL_S`)
— popular blobs cost one write an hour, not one per fetch; the upload counts
as a fetch, so a new blob is not the first to go.

Every node of a cluster shares the data dir and the database, so whichever
node took an upload, any node serves it. The upload rate is reserved
atomically in the shared database before any work (and refunded when the
upload fails), so it holds exactly across nodes. Making room, writing and
recording run one at a time per node (an :class:`asyncio.Lock`), so on one
node the byte caps hold exactly; across a cluster the total (or a scope's
bytes) may exceed its cap by the uploads other nodes have in flight — at
most (nodes − 1) × the largest blob — until the next upload's room making
brings it back under. Races with another node's delete or eviction are
safe: a blob row goes only while no claim holds it, its file only when the
row went; a claim onto a blob dropped meanwhile stores the bytes again, and
a row whose file went missing gets it back from the next upload of the same
bytes. The orphan-file sweep runs on one node at a time (a lease row) and
walks the shared directory a batch at a time. The bytes go through
:class:`.blob_store.BlobFiles` (files under ``media_dir/spaces`` today), so
an object store can replace the filesystem without touching this class.

Counters of evictions and refusals (:meth:`GfsSpaceMediaStore.stats`) are
per node and in memory, like the fan-out stats; the stored totals come from
the shared database.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..domain.gfs_space_media import MEDIA_SCOPES
from ..moment_public_media import MAX_BLOB_BYTES, is_blob_id
from .blob_store import BlobFiles, MediaRefused, check_address, check_shape
from .domain import GfsSpaceMedia, GfsSpaceMediaClaim

if TYPE_CHECKING:
    from .config import GfsConfig
    from .repositories import AbstractGfsSpaceMediaRepo

log = logging.getLogger(__name__)

#: A fetch moves a blob's ``last_fetched_at`` at most this often.
FETCH_TOUCH_INTERVAL_S: int = 3600

#: Rows read per eviction step.
EVICT_BATCH: int = 64

#: Expired blobs removed per prune step, and steps per sweep.
PRUNE_BATCH: int = 500
PRUNE_MAX_BATCHES: int = 20

#: A file with no row (or a temporary ``.part`` file) is removed by the
#: sweep only once it is this old — an upload may still be writing it.
ORPHAN_GRACE_S: int = 600

#: Directory entries the orphan sweep looks at per run (it resumes where
#: the last run stopped, so the whole directory is walked over successive
#: runs), and blob ids per row lookup.
ORPHAN_SCAN_BATCH: int = 100_000
_ORPHAN_LOOKUP_BATCH: int = 500

#: The orphan sweep's lease: one node of a cluster scans the shared data
#: dir, renewing each hourly run; another takes over when it lapses.
ORPHAN_LEASE_S: int = 3000

#: The upload rate window (a sliding hour over hourly buckets).
_HOUR: int = 3600


@dataclass(slots=True, frozen=True)
class SpaceMediaLimits:
    """The operator's bounds (``[space_media]``). ``space_*`` apply per
    scope — a listed space or a private space's channel."""

    max_bytes: int
    ttl_s: int
    space_max_bytes: int
    space_uploads_per_hour: int

    @classmethod
    def from_config(cls, cfg: GfsConfig) -> SpaceMediaLimits:
        return cls(
            max_bytes=cfg.space_media_max_bytes,
            ttl_s=cfg.space_media_ttl_seconds,
            space_max_bytes=cfg.space_media_space_max_bytes,
            space_uploads_per_hour=cfg.space_media_space_uploads_per_hour,
        )

    @property
    def enabled(self) -> bool:
        return self.max_bytes > 0

    @property
    def max_blob_bytes(self) -> int:
        """The largest blob this server takes."""
        return min(MAX_BLOB_BYTES, self.max_bytes, self.space_max_bytes)

    def capability(self) -> dict[str, object] | None:
        """The ``space_media`` entry of the signed ``/gfs/info`` capability
        block — what a household needs to decide whether, how long, how much
        and for which scopes to rely on this cache. ``None`` (not
        advertised) when it is off."""
        if not self.enabled:
            return None
        return {
            "max_blob_bytes": self.max_blob_bytes,
            "ttl_s": self.ttl_s,
            "space_max_bytes": self.space_max_bytes,
            "space_uploads_per_hour": self.space_uploads_per_hour,
            "scopes": sorted(MEDIA_SCOPES),
        }


class _Counters:
    """This node's eviction / refusal counts since it started."""

    __slots__ = ("evicted_blobs", "evicted_bytes", "refused_quota", "refused_too_large")

    def __init__(self) -> None:
        self.evicted_blobs = 0
        self.evicted_bytes = 0
        self.refused_quota = 0
        self.refused_too_large = 0


class GfsSpaceMediaStore:
    """Encrypted space blobs: bytes through :class:`BlobFiles`, metadata in
    ``gfs_space_media`` + ``gfs_space_media_claims``.

    One upload at a time per node makes room, writes and records (an
    :class:`asyncio.Lock`), after an atomic rate slot is reserved in the
    shared database — so on one node the caps hold exactly, and on a cluster
    the total may exceed them only by the uploads other nodes have in
    flight: at most (nodes − 1) × the largest blob."""

    __slots__ = ("_counters", "_files", "_limits", "_lock", "_node_id", "_repo")

    def __init__(
        self,
        repo: AbstractGfsSpaceMediaRepo,
        files: BlobFiles,
        limits: SpaceMediaLimits,
        *,
        node_id: str = "gfs",
    ) -> None:
        self._repo = repo
        self._files = files
        self._limits = limits
        self._counters = _Counters()
        self._lock = asyncio.Lock()
        #: Who holds the orphan sweep lease (``[cluster] node_id``).
        self._node_id = node_id

    @property
    def limits(self) -> SpaceMediaLimits:
        return self._limits

    def require_enabled(self) -> None:
        """``503 space_media_disabled`` while the operator has the cache off
        — not a refusal of this upload, so it is not counted."""
        if not self._limits.enabled:
            raise MediaRefused(503, "space_media_disabled")

    def admit_size(self, size: int) -> None:
        """Refuse a blob of *size* bytes this cache never takes (413
        ``too_large_for_cache``, counted) — the route asks before it reads
        a body, with the signed size."""
        self.require_enabled()
        if size > self._limits.max_blob_bytes:
            self._counters.refused_too_large += 1
            raise MediaRefused(413, "too_large_for_cache")

    async def put(
        self,
        *,
        scope_kind: str,
        scope_id: str,
        blob_id: str,
        data: bytes,
        expires_at: int,
        now: int | None = None,
    ) -> bool:
        """Keep one blob for the scope. ``True`` when its bytes were stored
        now, ``False`` when they already were (a retry, another scope's
        upload, another node winning the race) — the scope's claim is in
        place either way. Raises :class:`MediaRefused`."""
        self.require_enabled()
        if scope_kind not in MEDIA_SCOPES:
            raise MediaRefused(422, "bad_scope")
        now = int(time.time()) if now is None else now
        check_shape(blob_id, data)
        self.admit_size(len(data))
        if expires_at <= now:
            raise MediaRefused(422, "bad_expiry")
        expires_at = min(expires_at, now + self._limits.ttl_s)
        if await self._holds(blob_id, scope_kind, scope_id, now):
            # This scope already holds it: a no-op, and no fresher expiry.
            return False
        bucket = now // _HOUR
        await self._reserve_slot(scope_kind, scope_id, bucket, now)
        try:
            await check_address(blob_id, data)
            async with self._lock:
                stored, used = await self._store(
                    scope_kind, scope_id, blob_id, data, expires_at, now
                )
        except BaseException:
            await self._repo.refund_upload(scope_kind, scope_id, bucket=bucket)
            raise
        if not used:
            await self._repo.refund_upload(scope_kind, scope_id, bucket=bucket)
        return stored

    async def read(self, blob_id: str, *, now: int | None = None) -> bytes | None:
        """The blob, or ``None`` when unknown, expired, evicted, or held only
        by banned spaces. Marks the fetch at most once an hour."""
        if not is_blob_id(blob_id):
            return None
        now = int(time.time()) if now is None else now
        held = await self._repo.get_servable(blob_id, now=now)
        if held is None:
            return None
        data = await self._files.read(blob_id)
        if data is None:
            return None
        if now - held.last_fetched_at >= FETCH_TOUCH_INTERVAL_S:
            await self._repo.touch(
                blob_id, now=now, min_interval=FETCH_TOUCH_INTERVAL_S
            )
        return data

    async def delete(self, blob_id: str, *, scope_kind: str, scope_id: str) -> bool:
        """Drop the scope's claim (its seed holder asked). The blob and its
        file go only when no other scope still claims it."""
        if not is_blob_id(blob_id):
            return False
        async with self._lock:
            if not await self._repo.delete_claim(
                blob_id, scope_kind=scope_kind, scope_id=scope_id
            ):
                return False
            await self._settle(blob_id)
        return True

    async def prune_expired(self, now: int | None = None) -> int:
        """Drop expired blobs (rows, claims, files), expired claims of blobs
        another claim keeps, and claims whose blob is gone. Returns the
        blobs dropped."""
        now = int(time.time()) if now is None else now
        dropped = 0
        async with self._lock:
            for _ in range(PRUNE_MAX_BATCHES):
                ids = [
                    m.blob_id for m in await self._repo.expired(now, limit=PRUNE_BATCH)
                ]
                await self._drop(ids)
                dropped += len(ids)
                if len(ids) < PRUNE_BATCH:
                    break
            await self._repo.prune_expired_claims(now)
            await self._repo.prune_orphan_claims()
        return dropped

    async def sweep_orphans(self, *, now: float | None = None) -> int:
        """Remove, from the next batch of the directory in name order (the
        cursor persists, and wraps at the end — so successive sweeps walk
        the whole directory however large), files with no row and stale
        temporary files older than :data:`ORPHAN_GRACE_S`. Each is stat'ed
        and looked up again right before it goes, so a file an upload
        rewrote meanwhile is kept. Returns the files removed."""
        now = time.time() if now is None else now
        cutoff = now - ORPHAN_GRACE_S
        cursor = await self._repo.sweep_cursor()
        batch = await self._files.scan(after=cursor, limit=ORPHAN_SCAN_BATCH)
        await self._repo.set_sweep_cursor(
            batch[-1][0] if len(batch) >= ORPHAN_SCAN_BATCH else ""
        )
        old = [(n, m) for n, m in batch if m < cutoff]
        parts = [(n, m) for n, m in old if n.startswith(".") and n.endswith(".part")]
        blobs = [(n, m) for n, m in old if is_blob_id(n)]
        removed = 0
        for i in range(0, len(blobs), _ORPHAN_LOOKUP_BATCH):
            chunk = blobs[i : i + _ORPHAN_LOOKUP_BATCH]
            known = await self._repo.known_blob_ids([n for n, _ in chunk])
            for name, mtime in chunk:
                if (
                    name not in known
                    and await self._unchanged(name, mtime, cutoff)
                    # …and still no row, right before the unlink.
                    and not await self._repo.known_blob_ids([name])
                ):
                    await self._files.remove(name)
                    removed += 1
        for name, mtime in parts:
            if await self._unchanged(name, mtime, cutoff):
                await self._files.remove(name)
                removed += 1
        if removed:
            log.info("space media: removed %d orphan file(s)", removed)
        return removed

    async def sweep(self, now: int | None = None) -> int:
        """The maintenance sweep: expired blobs and claims, upload counts
        older than the rate window — on every node, it is all in the shared
        database — and, on the ONE node holding the sweep lease, a batch of
        the orphan-file scan. Returns the expired blobs dropped."""
        now = int(time.time()) if now is None else now
        dropped = await self.prune_expired(now)
        await self._repo.prune_upload_counts(before_bucket=now // _HOUR - 1)
        if await self._repo.acquire_sweep_lease(
            self._node_id, now=now, ttl=ORPHAN_LEASE_S
        ):
            await self.sweep_orphans()
        return dropped

    async def stats(self) -> dict[str, int | bool]:
        """What the admin overview shows: totals from the shared database,
        evictions and refusals of this node since it started."""
        blobs, stored = await self._repo.totals()
        c = self._counters
        return {
            "enabled": self._limits.enabled,
            "stored_blobs": blobs,
            "stored_bytes": stored,
            "max_bytes": self._limits.max_bytes,
            "ttl_seconds": self._limits.ttl_s,
            "evicted_blobs": c.evicted_blobs,
            "evicted_bytes": c.evicted_bytes,
            "refused_quota": c.refused_quota,
            "refused_too_large": c.refused_too_large,
        }

    # ── Internals ─────────────────────────────────────────────────────────

    async def _holds(
        self, blob_id: str, scope_kind: str, scope_id: str, now: int
    ) -> bool:
        """Whether the scope already holds a live claim on a live blob."""
        held = await self._repo.get_blob(blob_id)
        if held is None or held.expires_at <= now:
            return False
        return any(
            c.scope_kind == scope_kind and c.scope_id == scope_id and c.expires_at > now
            for c in await self._repo.claims_of(blob_id)
        )

    async def _reserve_slot(
        self, scope_kind: str, scope_id: str, bucket: int, now: int
    ) -> None:
        """Take one of ``space_uploads_per_hour`` in the sliding hour —
        atomically in the shared database (an upsert that only counts while
        under the cap), so concurrent uploads on any node can't all pass.
        The last hour weighs by how much of it still overlaps the window."""
        _, previous = await self._repo.upload_counts(
            scope_kind, scope_id, bucket=bucket
        )
        overlap = 1.0 - (now % _HOUR) / _HOUR
        cap = math.floor(self._limits.space_uploads_per_hour - previous * overlap)
        if not await self._repo.reserve_upload(
            scope_kind, scope_id, bucket=bucket, cap=cap
        ):
            self._counters.refused_quota += 1
            raise MediaRefused(429, "space_media_quota")

    async def _store(
        self,
        scope_kind: str,
        scope_id: str,
        blob_id: str,
        data: bytes,
        expires_at: int,
        now: int,
    ) -> tuple[bool, bool]:
        """Under the node's lock: make room, then claim (and store, when the
        bytes aren't here). Returns ``(stored, used the rate slot)``."""
        size = len(data)
        held = await self._repo.get_blob(blob_id)
        if held is not None and held.expires_at <= now:
            # Expired but not swept yet: start over.
            await self._drop([held.blob_id])
            held = None
        if held is not None and any(
            c.scope_kind == scope_kind and c.scope_id == scope_id and c.expires_at > now
            for c in await self._repo.claims_of(blob_id)
        ):
            # A concurrent upload of this scope got here first.
            return False, False
        await self._make_room_in_scope(scope_kind, scope_id, size, now)
        claim = GfsSpaceMediaClaim(
            blob_id=blob_id,
            scope_kind=scope_kind,
            scope_id=scope_id,
            size=size,
            created_at=now,
            expires_at=expires_at,
        )
        if held is not None:
            if not await self._files.exists(blob_id):
                # The row outlived its file (a crash or a race mid-drop): the
                # bytes are here, put them back.
                await self._files.write(blob_id, data)
            if await self._repo.put_claim(claim):
                return False, True
            # The row went between our read and our claim (dropped on another
            # node): store the bytes we have rather than claim nothing.
        await self._make_room(size, now)
        await self._files.write(blob_id, data)
        stored = await self._repo.add_blob(
            GfsSpaceMedia(
                blob_id=blob_id,
                size=size,
                created_at=now,
                expires_at=expires_at,
                last_fetched_at=now,
            )
        )
        # ``False`` when another node inserted it meanwhile: same bytes, so
        # ours is the coalesced answer and only the claim is new.
        await self._repo.put_claim(claim)
        return stored, True

    async def _unchanged(self, name: str, mtime: float, cutoff: float) -> bool:
        """The file still has the mtime it was listed with, and it is old."""
        current = await self._files.stat(name)
        return current is not None and current == mtime and current < cutoff

    async def _make_room_in_scope(
        self, scope_kind: str, scope_id: str, size: int, now: int
    ) -> None:
        """Free the scope's own budget until *size* more live bytes fit under
        ``space_max_bytes``: drop its claims on its least recently fetched,
        then oldest, blobs (expired claims count for nothing). A blob goes
        only when no other scope still claims it."""
        cap = self._limits.space_max_bytes
        live = await self._repo.scope_usage(scope_kind, scope_id, now=now)
        while live + size > cap:
            batch = await self._repo.scope_lru(
                scope_kind, scope_id, now=now, limit=EVICT_BATCH
            )
            if not batch:
                break
            for claim in batch:
                if live + size <= cap:
                    break
                await self._repo.delete_claim(
                    claim.blob_id, scope_kind=scope_kind, scope_id=scope_id
                )
                await self._settle(claim.blob_id)
                live -= claim.size
                self._count_eviction(claim.size)

    async def _make_room(self, size: int, now: int) -> None:
        """Free just enough of the whole cache for *size* more bytes under
        ``max_bytes``: expired blobs first, then least recently fetched,
        then oldest — every claim on them goes too."""
        _, total = await self._repo.totals()
        while total + size > self._limits.max_bytes:
            batch = await self._repo.expired(now, limit=EVICT_BATCH)
            evicting = not batch
            if evicting:
                batch = await self._repo.least_recently_fetched(limit=EVICT_BATCH)
            if not batch:
                break
            victims: list[str] = []
            for media in batch:
                if total + size <= self._limits.max_bytes:
                    break
                victims.append(media.blob_id)
                total -= media.size
                if evicting:
                    self._count_eviction(media.size)
            await self._drop(victims)

    async def _settle(self, blob_id: str) -> None:
        """After a claim went: the blob's expiry follows its other claims, or
        the blob goes — the row only while no claim holds it (one statement,
        so a claim another node adds meanwhile keeps it), the file only when
        that row went."""
        if await self._repo.settle_blob(blob_id) is not None:
            return
        if await self._repo.delete_blob_if_unclaimed(blob_id):
            await self._files.remove(blob_id)

    def _count_eviction(self, size: int) -> None:
        self._counters.evicted_blobs += 1
        self._counters.evicted_bytes += size

    async def _drop(self, blob_ids: list[str]) -> None:
        """Rows (and their claims) first, then files: a crash in between
        leaves a file with no row — the orphan sweep's — never a row whose
        file is gone."""
        await self._repo.delete_blobs(blob_ids)
        for blob_id in blob_ids:
            await self._files.remove(blob_id)
