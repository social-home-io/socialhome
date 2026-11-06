"""The GFS space media cache — encrypted, short-lived, bounded.

Households reached only through a GFS (a space member seated from an invite
link, a follower of a public / global space) can't fetch a picture from the
household that posted it. So a household encrypts each space file ONCE —
under the space content key, padded to a size bucket — and uploads the same
ciphertext ONCE per GFS; every reader fetches it here by its address. This
server can't open it.

**One blob, one scope.** A blob is uploaded for a scope — a listed space
(``space`` + its id) or a private space's opaque v_51 channel (``channel`` +
the random channel id; this server never learns which space that is) — and
its address is bound to that scope:
:func:`~socialhome.domain.gfs_space_media.space_media_address`,
``sha256("sm1|" + scope_kind + "|" + scope_id + "|" + ciphertext)``. This
server recomputes it on upload (``422 blob_id_mismatch`` otherwise) and a
reader checks it with the scope id it knows. The same ciphertext therefore
has another address in every scope: whoever fetched a space's blob can't
store it under their own scope at that space's address, so nothing another
scope does reaches it (security review v2, N1 — it replaced cross-scope
claims, owners and tombstones). The scope's own retries coalesce (``200
already_stored``, never a fresher expiry); its seed holder deletes it; a
moderator's ban of a space hides its blobs at once and drops them.

No tombstone keeps a deleted blob out: only the scope's own upload
authorization (its authority, a member's cert or writer key, its channel
key) can store it again — the same parties who may upload any new content of
that scope — and the item that named it is gone.

It is a **cache, not storage** — the operator sizes it to the disk they have
(``[space_media]`` in the GFS config, :class:`SpaceMediaLimits`), and it
favours new pictures over old ones:

* a blob lives at most ``ttl_seconds`` (3 days by default), whatever expiry
  the uploader asks; a retry does NOT extend it. The maintenance sweep
  (:meth:`GfsSpaceMediaStore.sweep`) drops expired blobs and upload counts,
  and files with no row;
* one scope holds at most ``space_max_bytes`` (256 MiB): when a new blob
  would pass it, that scope's own **least recently fetched** (then oldest)
  blobs are evicted until it fits;
* all blobs together stay under ``max_bytes`` (5 GiB): before a new blob is
  stored, expired blobs go first, then — fair PER HOUSEHOLD, never one
  global order and never per scope — the least recently fetched (then
  oldest) blobs of the household holding the most bytes across all its
  scopes (a space's owning instance, a channel's oldest seated member
  household), down to the next largest, then again — so a household
  flooding the cache, with however many spaces or channels, pushes out its
  own (security review F1, third review A3); only an ACTIVE listed space
  takes uploads;
* one client address uploads at most ``ip_bytes_per_hour`` (1 GiB) across
  every scope (``429 ip_media_quota``), cluster-wide in the shared database
  under a keyed hash of the address; a channel takes media only while it is
  used by enough member households (the auth's check);
* a blob larger than either budget is ``413``; a scope accepting more than
  ``space_uploads_per_hour`` uploads (a sliding hour, counted in
  ``gfs_space_media_uploads`` — eviction and delete can't reset it) is
  ``429 space_media_quota``; a cache the operator turned off is ``503
  space_media_disabled``.

A fetch marks the blob at most once an hour (:data:`FETCH_TOUCH_INTERVAL_S`)
— popular blobs cost one write an hour, not one per fetch; the upload counts
as a fetch, so a new blob is not the first to go.

Every node of a cluster shares the data dir and the database, so whichever
node took an upload, any node serves it. The upload rate and the address
budget are reserved atomically in the shared database before the body is
read (and given back when the upload fails), so they hold exactly across
nodes. Making room, writing and recording run one at a time per node (an
:class:`asyncio.Lock`), so on one node the byte caps hold exactly; across a
cluster the total (or a scope's bytes) may exceed its cap by the uploads
other nodes have in flight — at most (nodes − 1) × the largest blob — until
the next upload's room making brings it back under. Rows go before files,
and a file only when its row went; a row whose file went missing gets it
back from the scope's next upload of it.

Cross-node windows that remain, all narrow and self-healing (the household
side re-uploads on a 404): (1) node A drops a row, node B stores the same
blob again (row + file), then A's file removal lands — B answered 201, the
row has no file until the scope's next upload of it rewrites it; (2) node B
answers a retry ``already_stored`` while node A evicts or expires that blob;
(3) the orphan sweep re-checks a file's mtime and row and unlinks it under
its node's lock, but an upload on ANOTHER node could store that blob between
the re-check and the unlink. The orphan-file sweep runs on one node at a
time (a lease row) and walks the shared directory a batch at a time. The
bytes go through :class:`.blob_store.BlobFiles` (files under
``data_dir/space_media`` today — never under the public ``media/`` mount),
so an object store can replace the filesystem without touching this class.

Counters of evictions and refusals (:meth:`GfsSpaceMediaStore.stats`) are
per node and in memory, like the fan-out stats; the stored totals come from
the shared database.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..domain.gfs_space_media import MEDIA_SCOPES, space_media_address
from ..moment_public_media import MAX_BLOB_BYTES, is_blob_id
from .blob_store import (
    TEMP_GRACE_S,
    BlobFiles,
    MediaRefused,
    check_shape,
    check_size,
    remove_stale_temps,
)
from .domain import GfsSpaceMedia

if TYPE_CHECKING:
    from .config import GfsConfig
    from .repositories import AbstractGfsSpaceMediaRepo

log = logging.getLogger(__name__)

#: A fetch moves a blob's ``last_fetched_at`` at most this often.
FETCH_TOUCH_INTERVAL_S: int = 3600

#: Rows read per eviction step.
EVICT_BATCH: int = 64

#: Eviction steps per upload at most.
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

#: Upload bodies one client address bucket may have in flight on a node at
#: once — so one client stalling header-only uploads holds this many, never
#: the node (third review A5).
PER_CLIENT_BODY_READS: int = 3

#: Body bytes (signed sizes) one node buffers at once across all clients;
#: past it an upload is ``503 busy`` rather than another buffer.
MAX_BODY_BYTES_IN_FLIGHT: int = 256 * 1024 * 1024


@dataclass(slots=True, frozen=True)
class SpaceMediaLimits:
    """The operator's bounds (``[space_media]``). ``space_*`` apply per
    scope — a listed space or a private space's channel."""

    max_bytes: int
    ttl_s: int
    space_max_bytes: int
    space_uploads_per_hour: int
    #: Bytes one client address may upload an hour, across every scope.
    ip_bytes_per_hour: int = 1024**3

    @classmethod
    def from_config(cls, cfg: GfsConfig) -> SpaceMediaLimits:
        return cls(
            max_bytes=cfg.space_media_max_bytes,
            ttl_s=cfg.space_media_ttl_seconds,
            space_max_bytes=cfg.space_media_space_max_bytes,
            space_uploads_per_hour=cfg.space_media_space_uploads_per_hour,
            ip_bytes_per_hour=cfg.space_media_ip_bytes_per_hour,
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


@dataclass(slots=True, frozen=True)
class UploadSlot:
    """What :meth:`GfsSpaceMediaStore.admit_upload` reserved for one upload
    before its body was read: the scope's rate slot (``bucket``) and, with
    a client, its address's bytes (``ip_key``, ``ip_bytes``). Store-local:
    handed back to :meth:`~GfsSpaceMediaStore.put` or
    :meth:`~GfsSpaceMediaStore.release_upload`, never stored."""

    scope_kind: str
    scope_id: str
    bucket: int
    ip_key: str | None = None
    ip_bytes: int = 0


class BodyReadGate:
    """Which upload bodies a node reads right now: at most
    :data:`PER_CLIENT_BODY_READS` per client address bucket, at most
    :data:`MAX_BODY_BYTES_IN_FLIGHT` signed bytes in all. Non-blocking: a
    refused :meth:`enter` is answered ``503 busy`` at once, never queued
    (third review A5)."""

    __slots__ = ("_by_client", "_max_bytes", "_per_client", "in_flight")

    def __init__(
        self,
        *,
        per_client: int = PER_CLIENT_BODY_READS,
        max_bytes: int = MAX_BODY_BYTES_IN_FLIGHT,
    ) -> None:
        self._per_client = per_client
        self._max_bytes = max_bytes
        self._by_client: dict[str, int] = {}
        #: Signed bytes of the bodies being read now.
        self.in_flight = 0

    def enter(self, client_key: str, size: int) -> bool:
        if self._by_client.get(client_key, 0) >= self._per_client:
            return False
        if self.in_flight + size > self._max_bytes:
            return False
        self._by_client[client_key] = self._by_client.get(client_key, 0) + 1
        self.in_flight += size
        return True

    def leave(self, client_key: str, size: int) -> None:
        left = self._by_client.get(client_key, 0) - 1
        if left > 0:
            self._by_client[client_key] = left
        else:
            self._by_client.pop(client_key, None)
        self.in_flight = max(0, self.in_flight - size)


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
    ``gfs_space_media`` (one row per blob, each in one scope).

    One upload at a time per node makes room, writes and records (an
    :class:`asyncio.Lock`), after an atomic rate slot is reserved in the
    shared database — so on one node the caps hold exactly, and on a cluster
    the total may exceed them only by the uploads other nodes have in
    flight: at most (nodes − 1) × the largest blob."""

    __slots__ = (
        "_counters",
        "_files",
        "_ip_key_secret",
        "_limits",
        "_lock",
        "_node_id",
        "_repo",
        "body_gate",
    )

    def __init__(
        self,
        repo: AbstractGfsSpaceMediaRepo,
        files: BlobFiles,
        limits: SpaceMediaLimits,
        *,
        node_id: str = "gfs",
        ip_key_secret: bytes | None = None,
    ) -> None:
        self._repo = repo
        self._files = files
        self._limits = limits
        self._counters = _Counters()
        self._lock = asyncio.Lock()
        #: Who holds the orphan sweep lease (``[cluster] node_id``).
        self._node_id = node_id
        #: Keys the per-address byte budget's hash (the cluster shares one,
        #: derived from the shared signing seed), so the database never
        #: holds a client address.
        self._ip_key_secret = ip_key_secret or secrets.token_bytes(32)
        #: Bounds the upload bodies this node reads at once (the route).
        self.body_gate = BodyReadGate()

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
        client: str | None = None,
    ) -> UploadSlot | None:
        """Everything an upload can be refused for BEFORE its body is read
        (the route calls it with the signed size): the cache is on, the
        scope is one, the size is a padded bucket this cache takes, and —
        unless the scope already holds the blob with its file (``None``: a
        retry, answered without reading anything) — one of the scope's
        upload slots and, for a *client* address, *size* bytes of its hourly
        budget, both reserved atomically. Returns the :class:`UploadSlot`,
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
        held = await self._repo.get_blob(blob_id)
        if (
            held is not None
            and held.expires_at > now
            and (held.scope_kind, held.scope_id) == (scope_kind, scope_id)
            and await self._files.exists(blob_id)
        ):
            # The scope already holds it: a no-op, and no fresher expiry.
            # (A row whose file went missing takes the locked path of
            # :meth:`put`, which checks the bytes' address and puts them
            # back.)
            return None
        bucket = now // _HOUR
        await self._reserve_slot(scope_kind, scope_id, bucket, now)
        slot = UploadSlot(scope_kind=scope_kind, scope_id=scope_id, bucket=bucket)
        if client is None:
            return slot
        ip_key = self._ip_key(client)
        try:
            await self._reserve_ip_bytes(ip_key, bucket, size, now)
        except MediaRefused:
            await self._repo.refund_upload(scope_kind, scope_id, bucket=bucket)
            raise
        return UploadSlot(
            scope_kind=scope_kind,
            scope_id=scope_id,
            bucket=bucket,
            ip_key=ip_key,
            ip_bytes=size,
        )

    async def release_upload(self, slot: UploadSlot) -> None:
        """Give back what :meth:`admit_upload` reserved for an upload that
        never reached :meth:`put` (its body was too large, short or slow)."""
        await self._repo.refund_upload(
            slot.scope_kind, slot.scope_id, bucket=slot.bucket
        )
        await self._refund_ip(slot)

    async def put(
        self,
        *,
        scope_kind: str,
        scope_id: str,
        blob_id: str,
        data: bytes,
        expires_at: int,
        now: int | None = None,
        reserved: UploadSlot | None = None,
        client: str | None = None,
        household: str | None = None,
    ) -> bool:
        """Keep one blob for the scope. ``True`` when its bytes were stored
        now, ``False`` when they already were (the scope's retry, another
        node winning the race). *blob_id* must be the scope-bound address of
        *data* (``422 blob_id_mismatch``). *reserved* is the slot
        :meth:`admit_upload` took (given back here when the upload fails or
        stores nothing new); without it this call admits the upload itself
        (for *client*'s address). *household* is the household the scope
        belongs to (the auth says which) — the fairness key; without it the
        scope stands alone. Raises :class:`MediaRefused`."""
        now = int(time.time()) if now is None else now
        if reserved is None:
            admitted = await self.admit_upload(
                scope_kind=scope_kind,
                scope_id=scope_id,
                blob_id=blob_id,
                size=len(data),
                now=now,
                client=client,
            )
            if admitted is None:
                return False
            slot = admitted
        else:
            slot = reserved
        try:
            check_shape(blob_id, data)
            if expires_at <= now:
                raise MediaRefused(422, "bad_expiry")
            expires_at = min(expires_at, now + self._limits.ttl_s)
            await check_scoped_address(scope_kind, scope_id, blob_id, data)
            async with self._lock:
                stored = await self._store(
                    scope_kind,
                    scope_id,
                    blob_id,
                    data,
                    expires_at,
                    now,
                    household or f"{scope_kind}:{scope_id}",
                )
        except BaseException:
            await self.release_upload(slot)
            raise
        if not stored:
            # Nothing new was stored: neither the scope's rate nor the
            # address's budget is charged.
            await self.release_upload(slot)
        return stored

    async def read(self, blob_id: str, *, now: int | None = None) -> bytes | None:
        """The blob, or ``None`` when unknown, expired, evicted, or its
        scope is a banned space. Marks the fetch at most once an hour."""
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
        """The scope's seed holder deletes one of the scope's blobs (row,
        then file). Another scope's blob is not theirs: ``False``."""
        if not is_blob_id(blob_id):
            return False
        async with self._lock:
            if not await self._repo.delete_scope_blob(
                blob_id, scope_kind=scope_kind, scope_id=scope_id
            ):
                return False
            await self._files.remove(blob_id)
        return True

    async def withdraw_scope(self, scope_kind: str, scope_id: str) -> int:
        """A moderator banned the scope: every blob of it goes (rows, then
        files). Its blobs are hidden from the moment of the ban already
        (:meth:`read` checks the space's status); its uploads are refused by
        the auth. Returns the blobs dropped."""
        async with self._lock:
            ids = await self._repo.scope_blob_ids(scope_kind, scope_id)
            dropped = await self._drop(ids)
        if dropped:
            log.info("space media: dropped %d blob(s) of a banned scope", len(dropped))
        return len(dropped)

    async def prune_expired(self, now: int | None = None) -> int:
        """Drop expired blobs (rows, then files). The node's lock is taken
        per batch, so uploads wait for one batch, not the sweep. Returns
        the blobs dropped."""
        now = int(time.time()) if now is None else now
        dropped = 0
        for _ in range(PRUNE_MAX_BATCHES):
            async with self._lock:
                ids = [
                    m.blob_id for m in await self._repo.expired(now, limit=PRUNE_BATCH)
                ]
                dropped += len(await self._drop(ids))
            if len(ids) < PRUNE_BATCH:
                break
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
        """The maintenance sweep: expired blobs, upload counts and address
        budgets older than the rate window — on every node, it is all in the
        shared database — and, on the ONE node holding the sweep lease, a
        batch of the orphan-file scan. Returns the expired blobs dropped."""
        now = int(time.time()) if now is None else now
        dropped = await self.prune_expired(now)
        await self._repo.prune_upload_counts(before_bucket=now // _HOUR - 1)
        await self._repo.prune_ip_bytes(before_bucket=now // _HOUR - 1)
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

    def client_key(self, client: str) -> str:
        """The keyed hash of a client address bucket — the per-address
        budget's key, and the body-read gate's."""
        return self._ip_key(client)

    def _ip_key(self, client: str) -> str:
        """The per-address budget's key: a keyed hash of the address bucket
        — the database never holds the address."""
        return hashlib.blake2b(
            client.encode(), key=self._ip_key_secret, digest_size=16
        ).hexdigest()

    async def _reserve_ip_bytes(
        self, ip_key: str, bucket: int, size: int, now: int
    ) -> None:
        """Take *size* bytes of the address's ``ip_bytes_per_hour`` in the
        sliding hour (the last hour weighed by its overlap) — atomically in
        the shared database, cluster-wide."""
        _, previous = await self._repo.ip_bytes(ip_key, bucket=bucket)
        overlap = 1.0 - (now % _HOUR) / _HOUR
        cap = math.floor(self._limits.ip_bytes_per_hour - previous * overlap)
        if not await self._repo.reserve_ip_bytes(
            ip_key, bucket=bucket, size=size, cap=cap
        ):
            self._counters.refused_quota += 1
            raise MediaRefused(429, "ip_media_quota")

    async def _refund_ip(self, slot: UploadSlot) -> None:
        if slot.ip_key is not None and slot.ip_bytes:
            await self._repo.refund_ip_bytes(
                slot.ip_key, bucket=slot.bucket, size=slot.ip_bytes
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
        household: str,
    ) -> bool:
        """Under the node's lock: make room, then store — unless the scope
        already holds it (its retry, or another node's concurrent upload of
        it). Returns whether the bytes were stored now."""
        size = len(data)
        held = await self._repo.get_blob(blob_id)
        if held is not None and held.expires_at <= now:
            # Expired but not swept yet: start over.
            await self._drop([blob_id])
            held = None
        if held is not None:
            # The scope's retry (the address binds the scope). A row that
            # outlived its file — a cross-node window — gets the bytes back,
            # so already_stored means servable.
            if not await self._files.exists(blob_id):
                await self._files.write(blob_id, data)
            return False
        await self._make_room_in_scope(scope_kind, scope_id, size, now)
        await self._make_room(size, now, household)
        await self._files.write(blob_id, data)
        # ``False`` when another node inserted it meanwhile: same scope, same
        # bytes — ours is the coalesced answer.
        return await self._repo.add_blob(
            GfsSpaceMedia(
                blob_id=blob_id,
                size=size,
                created_at=now,
                expires_at=expires_at,
                last_fetched_at=now,
                scope_kind=scope_kind,
                scope_id=scope_id,
                household=household,
            )
        )

    async def _unchanged(self, name: str, mtime: float, cutoff: float) -> bool:
        """The file still has the mtime it was listed with, and it is old."""
        current = await self._files.stat(name)
        return current is not None and current == mtime and current < cutoff

    async def _make_room_in_scope(
        self, scope_kind: str, scope_id: str, size: int, now: int
    ) -> None:
        """Free the scope's own budget until *size* more bytes fit under
        ``space_max_bytes``: its least recently fetched, then oldest, blobs
        go first (an expired one is not counted as an eviction)."""
        cap = self._limits.space_max_bytes
        for _ in range(EVICT_MAX_ROUNDS):
            used = await self._repo.scope_bytes(scope_kind, scope_id)
            if used + size <= cap:
                return
            batch = await self._repo.scope_lru(scope_kind, scope_id, limit=EVICT_BATCH)
            if not batch:
                return
            victims: list[GfsSpaceMedia] = []
            for media in batch:
                if used + size <= cap:
                    break
                victims.append(media)
                used -= media.size
            await self._evict(victims, now)

    async def _make_room(self, size: int, now: int, household: str) -> None:
        """Free just enough of the whole cache for *size* more bytes under
        ``max_bytes``: expired blobs first, then — FAIR PER HOUSEHOLD, never
        one global order and never per scope — the least recently fetched
        (then oldest) blobs of the household holding the most bytes across
        all its scopes, counting the uploader's household with the blob it
        is storing (a tie takes the uploader's own), until it is down to the
        next largest, then again. So a household flooding the cache — however
        many spaces or channels it uses — pushes out its own blobs, and every
        other household keeps its share (security review F1, third review
        A3)."""
        for _ in range(EVICT_MAX_ROUNDS):
            _, total = await self._repo.totals()
            if total + size <= self._limits.max_bytes:
                return
            expired = await self._repo.expired(now, limit=EVICT_BATCH)
            if expired:
                picked = self._just_enough(expired, total, size, floor=None)
                await self._drop([m.blob_id for m in picked])
                continue
            held = {h: b for h, b in await self._repo.largest_households(limit=2)}
            held[household] = await self._repo.household_bytes(household) + size
            # The largest first; on a tie, the uploader's own household.
            ranked = sorted(held.items(), key=lambda hb: (-hb[1], hb[0] != household))
            victims: list[GfsSpaceMedia] = []
            for i, (victim, victim_bytes) in enumerate(ranked):
                batch = await self._repo.household_lru(victim, limit=EVICT_BATCH)
                if not batch:
                    continue  # the uploader's first blob: nothing of its own
                floor = ranked[i + 1][1] if i + 1 < len(ranked) else 0
                victims = self._just_enough(
                    batch, total, size, floor=(victim_bytes, floor)
                )
                break
            if not victims:  # pragma: no cover - some household holds bytes
                return
            await self._evict(victims, now)

    def _just_enough(
        self,
        batch: list[GfsSpaceMedia],
        total: int,
        size: int,
        *,
        floor: tuple[int, int] | None,
    ) -> list[GfsSpaceMedia]:
        """The first blobs of *batch* that bring the total under the cap —
        at least one; with *floor* ``(household bytes, next largest)``, stop
        once the household is down to the next largest."""
        picked: list[GfsSpaceMedia] = []
        freed = 0
        for media in batch:
            if total - freed + size <= self._limits.max_bytes:
                break
            if floor is not None and picked and floor[0] - freed <= floor[1]:
                break
            picked.append(media)
            freed += media.size
        return picked

    async def _evict(self, victims: list[GfsSpaceMedia], now: int) -> None:
        dropped = set(await self._drop([m.blob_id for m in victims]))
        for media in victims:
            if media.blob_id in dropped and media.expires_at > now:
                self._counters.evicted_blobs += 1
                self._counters.evicted_bytes += media.size

    async def _drop(self, blob_ids: list[str]) -> list[str]:
        """Rows first, then the files of the rows that went: a crash in
        between leaves a file with no row — the orphan sweep's — never a row
        whose file is gone. Returns the ids dropped."""
        dropped = await self._repo.delete_blobs(blob_ids)
        for blob_id in dropped:
            await self._files.remove(blob_id)
        return dropped


async def check_scoped_address(
    scope_kind: str, scope_id: str, blob_id: str, data: bytes
) -> None:
    """``422 blob_id_mismatch`` unless *blob_id* is the scope-bound address
    of *data* (:func:`~socialhome.domain.gfs_space_media.space_media_address`)
    — hashing up to 8 MiB in a worker thread."""
    try:
        address = await asyncio.to_thread(
            space_media_address, scope_kind, scope_id, data
        )
    except ValueError as exc:
        raise MediaRefused(422, "bad_scope") from exc
    if address != blob_id:
        raise MediaRefused(422, "blob_id_mismatch")
