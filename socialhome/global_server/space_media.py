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
as a fetch — a new blob, and a new claim coalescing onto a held one — so
what a household was just told is there is not the first to go.

Every node of a cluster shares the data dir and the database, so whichever
node took an upload, any node serves it. The upload rate is reserved
atomically in the shared database before any work (and refunded when the
upload fails), so it holds exactly across nodes. Making room, writing and
recording run one at a time per node (an :class:`asyncio.Lock`), so on one
node the byte caps hold exactly; across a cluster the total (or a scope's
bytes) may exceed its cap by the uploads other nodes have in flight — at
most (nodes − 1) × the largest blob — until the next upload's room making
brings it back under. Deletes, eviction and the expiry sweep are
claim-aware: a delete drops one claim and the row only while no claim holds
it; an eviction or expiry notes the newest claim's sequence number before it
reads its candidates and drops only claims up to it (or expired), so a blob
another node claimed meanwhile survives (and eviction takes the next
candidate); a file goes only when its row went, rows before files. A claim
onto a blob whose row was dropped meanwhile stores the bytes again, and a
row whose file went missing gets it back from the next upload of the same
bytes.

Two cross-node windows remain, both narrow and self-healing: (1) node A
drops a row, node B stores the same blob again (row + file), and then A's
file removal lands — B answered 201, but the row has no file until the next
upload of the same bytes rewrites it (any scope's, the uploader's own retry
included; the household side re-uploads on a 404); (2)
the orphan sweep re-checks a file's mtime and row and unlinks it under its
node's lock, but an upload on ANOTHER node could store that blob between the
re-check and the unlink — the same row-without-file, healed the same way.
The orphan-file sweep runs on one node at a time (a lease row) and walks the
shared directory a batch at a time. The bytes go through
:class:`.blob_store.BlobFiles` (files under ``data_dir/space_media`` today — never under the public ``media/`` mount), so
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
from .blob_store import (
    TEMP_GRACE_S,
    BlobFiles,
    MediaRefused,
    check_address,
    check_shape,
    check_size,
    remove_stale_temps,
)
from .domain import GfsSpaceMedia, GfsSpaceMediaClaim

if TYPE_CHECKING:
    from .config import GfsConfig
    from .repositories import AbstractGfsSpaceMediaRepo

log = logging.getLogger(__name__)

#: A fetch moves a blob's ``last_fetched_at`` at most this often.
FETCH_TOUCH_INTERVAL_S: int = 3600

#: Rows read per eviction step.
EVICT_BATCH: int = 64

#: Eviction steps per upload at most (a step whose victims another node
#: claimed meanwhile takes the next candidates).
EVICT_MAX_ROUNDS: int = 64

#: Expired blobs removed per prune step, and steps per sweep.
PRUNE_BATCH: int = 500
PRUNE_MAX_BATCHES: int = 20

#: A file with no row (or a temporary ``.part`` file) is removed by the
#: sweep only once it is this old — an upload may still be writing it.
ORPHAN_GRACE_S: int = TEMP_GRACE_S

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

    async def admit_upload(
        self,
        *,
        scope_kind: str,
        scope_id: str,
        blob_id: str,
        size: int,
        now: int | None = None,
    ) -> int | None:
        """Everything an upload can be refused for BEFORE its body is read
        (the route calls it with the signed size): the cache is on, the
        scope is one, the size is a padded bucket this cache takes, and —
        unless the scope already holds the blob with its file (``None``: a
        retry, answered without reading anything) — one of the scope's
        upload slots, reserved atomically. Returns the slot's hour bucket,
        for :meth:`put` (``reserved=``) or :meth:`release_upload`. Raises
        :class:`MediaRefused`."""
        self.require_enabled()
        if scope_kind not in MEDIA_SCOPES:
            raise MediaRefused(422, "bad_scope")
        if not is_blob_id(blob_id):
            raise MediaRefused(422, "bad_blob_id")
        check_size(size)
        self.admit_size(size)
        now = int(time.time()) if now is None else now
        if await self._holds(
            blob_id, scope_kind, scope_id, now
        ) and await self._files.exists(blob_id):
            # This scope already holds it: a no-op, and no fresher expiry.
            # (A row whose file went missing takes the locked path of
            # :meth:`put`, which checks the bytes' address and puts them
            # back.)
            return None
        bucket = now // _HOUR
        await self._reserve_slot(scope_kind, scope_id, bucket, now)
        return bucket

    async def release_upload(self, scope_kind: str, scope_id: str, bucket: int) -> None:
        """Give back a slot :meth:`admit_upload` reserved for an upload that
        never reached :meth:`put` (its body was too large, short or slow)."""
        await self._repo.refund_upload(scope_kind, scope_id, bucket=bucket)

    async def put(
        self,
        *,
        scope_kind: str,
        scope_id: str,
        blob_id: str,
        data: bytes,
        expires_at: int,
        now: int | None = None,
        reserved: int | None = None,
    ) -> bool:
        """Keep one blob for the scope. ``True`` when its bytes were stored
        now, ``False`` when they already were (a retry, another scope's
        upload, another node winning the race) — the scope's claim is in
        place either way. *reserved* is the slot :meth:`admit_upload` took
        (refunded here when the upload fails or stores nothing new); without
        it this call admits the upload itself. Raises :class:`MediaRefused`."""
        now = int(time.time()) if now is None else now
        if reserved is None:
            admitted = await self.admit_upload(
                scope_kind=scope_kind,
                scope_id=scope_id,
                blob_id=blob_id,
                size=len(data),
                now=now,
            )
            if admitted is None:
                return False
            bucket = admitted
        else:
            bucket = reserved
        try:
            check_shape(blob_id, data)
            if expires_at <= now:
                raise MediaRefused(422, "bad_expiry")
            expires_at = min(expires_at, now + self._limits.ttl_s)
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
        another claim keeps, and claims whose blob is gone. The node's lock
        is taken per batch, so uploads wait for one batch, not the sweep.
        A blob another node claimed meanwhile survives (its expiry moved).
        Returns the blobs dropped."""
        now = int(time.time()) if now is None else now
        dropped = 0
        for _ in range(PRUNE_MAX_BATCHES):
            async with self._lock:
                mark = await self._repo.claim_mark()
                ids = [
                    m.blob_id for m in await self._repo.expired(now, limit=PRUNE_BATCH)
                ]
                dropped += len(await self._drop(ids, mark=mark, now=now))
            if len(ids) < PRUNE_BATCH:
                break
        async with self._lock:
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
        batch, next_after = await self._files.scan(
            after=cursor, limit=ORPHAN_SCAN_BATCH
        )
        # The last name listed, not the last one stat'ed: a file that went
        # between the two must not send the walk back to the start.
        await self._repo.set_sweep_cursor(next_after)
        blobs = [(n, m) for n, m in batch if m < cutoff and is_blob_id(n)]
        removed = await remove_stale_temps(self._files, batch, cutoff=cutoff)
        for i in range(0, len(blobs), _ORPHAN_LOOKUP_BATCH):
            chunk = blobs[i : i + _ORPHAN_LOOKUP_BATCH]
            known = await self._repo.known_blob_ids([n for n, _ in chunk])
            for name, mtime in chunk:
                if name in known:
                    continue
                # The last check and the unlink under the node's lock, so no
                # upload on this node stores the blob in between.
                async with self._lock:
                    if await self._unchanged(
                        name, mtime, cutoff
                    ) and not await self._repo.known_blob_ids([name]):
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
        mark = await self._repo.claim_mark()
        held = await self._repo.get_blob(blob_id)
        if held is not None and held.expires_at <= now:
            # Expired but not swept yet: start over — unless another node
            # claimed it meanwhile (then it is live again).
            if await self._drop([held.blob_id], mark=mark, now=now):
                held = None
            else:
                held = await self._repo.get_blob(blob_id)
        if held is not None and any(
            c.scope_kind == scope_kind and c.scope_id == scope_id and c.expires_at > now
            for c in await self._repo.claims_of(blob_id)
        ):
            # A concurrent upload of this scope got here first (or its
            # retry). A row that outlived its file — the cross-node window —
            # gets the bytes back, so already_stored means servable.
            if not await self._files.exists(blob_id):
                await self._files.write(blob_id, data)
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
        then oldest — every claim on them goes too, except a claim another
        node made since this eviction started: that blob survives and the
        next candidate is taken instead."""
        survivors: set[str] = set()
        _, total = await self._repo.totals()
        for _ in range(EVICT_MAX_ROUNDS):
            if total + size <= self._limits.max_bytes:
                return
            # Claims made after this mark (another node coalescing onto a
            # candidate while we pick it) keep their blob.
            mark = await self._repo.claim_mark()
            batch = [
                m
                for m in await self._repo.expired(now, limit=EVICT_BATCH)
                if m.blob_id not in survivors
            ]
            evicting = not batch
            if evicting:
                batch = [
                    m
                    for m in await self._repo.least_recently_fetched(
                        limit=EVICT_BATCH + len(survivors)
                    )
                    if m.blob_id not in survivors
                ]
            if not batch:
                return
            victims: list[GfsSpaceMedia] = []
            freed = 0
            for media in batch:
                if total - freed + size <= self._limits.max_bytes:
                    break
                victims.append(media)
                freed += media.size
            dropped = set(
                await self._drop([m.blob_id for m in victims], mark=mark, now=now)
            )
            for media in victims:
                if media.blob_id not in dropped:
                    survivors.add(media.blob_id)
                elif evicting:
                    self._count_eviction(media.size)
            _, total = await self._repo.totals()

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

    async def _drop(self, blob_ids: list[str], *, mark: int, now: int) -> list[str]:
        """Drop blobs for an eviction or expiry that noted the claim *mark*
        before it read them, claim-aware
        (:meth:`~.repositories.SqliteGfsSpaceMediaRepo.drop_blobs`): a blob
        another node claimed since then keeps its row and its file. Rows
        first, then the files of the rows that went: a crash in between
        leaves a file with no row — the orphan sweep's — never a row whose
        file is gone. Returns the ids dropped."""
        dropped = await self._repo.drop_blobs(blob_ids, mark=mark, now=now)
        for blob_id in dropped:
            await self._files.remove(blob_id)
        return dropped
