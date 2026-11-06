"""The GFS space media cache — encrypted, short-lived, bounded.

Households reached only through a GFS (a space member seated from an invite
link, a follower of a public / global space) can't fetch a picture from the
household that posted it. So a household encrypts each space file ONCE —
under the space content key, padded to a size bucket — and uploads the same
ciphertext ONCE per GFS; every reader fetches it here by its SHA-256. This
server can't open it.

Each blob belongs to a **scope**, the key of its caps and of its delete: a
listed space (``space`` + its id) or a private space's opaque v_51 channel
(``channel`` + the random channel id — this server never learns which space
that is).

It is a **cache, not storage** — the operator sizes it to the disk they have
(``[space_media]`` in the GFS config, :class:`SpaceMediaLimits`), and it
favours new pictures over old ones:

* a blob lives at most ``ttl_seconds`` (3 days by default), whatever expiry
  the uploader asks; the maintenance sweep drops expired rows and files;
* one scope holds at most ``space_max_bytes`` live (256 MiB): when a new blob
  would pass it, that scope's own **least recently fetched** (then oldest)
  blobs are evicted until it fits — a busy space keeps its newest pictures
  instead of being refused;
* all blobs together stay under ``max_bytes`` (5 GiB): expired ones go
  first, then the least recently fetched (then oldest) of any scope;
* a blob larger than either budget is ``413``; a scope uploading more than
  ``space_uploads_per_hour`` (300) blobs an hour is ``429
  space_media_quota`` — the only refusal by quota.

A fetch marks the blob at most once an hour (:data:`FETCH_TOUCH_INTERVAL_S`)
— popular blobs cost one write an hour, not one per fetch; the upload counts
as a fetch, so a new blob is not the first to go.

A re-upload of a held blob is a no-op (``False``), never an error: one file,
one blob, however often it is retried.

Every node of a cluster shares the data dir and the database, so whichever
node took an upload, any node serves it. The caps are checked per upload
against the shared rows; two nodes evicting at once may free a little more
than needed, never less. The bytes go through :class:`.blob_store.BlobFiles`
(files under ``media_dir/spaces`` today), so an object store can replace the
filesystem without touching this class.

Counters of evictions and refusals (:meth:`GfsSpaceMediaStore.stats`) are
per node and in memory, like the fan-out stats; the stored totals come from
the shared database.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..domain.gfs_space_media import MEDIA_SCOPES
from ..moment_public_media import MAX_BLOB_BYTES, is_blob_id
from .blob_store import BlobFiles, MediaRefused, check_address, check_shape
from .domain import GfsSpaceMedia

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

#: The upload rate window.
_RATE_WINDOW_S: int = 3600


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
        block — what a household needs to decide whether, how long and for
        which scopes to rely on this cache. ``None`` (not advertised) when
        it is off."""
        if not self.enabled:
            return None
        return {
            "max_blob_bytes": self.max_blob_bytes,
            "ttl_s": self.ttl_s,
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
    ``gfs_space_media``."""

    __slots__ = ("_counters", "_files", "_limits", "_repo")

    def __init__(
        self,
        repo: AbstractGfsSpaceMediaRepo,
        files: BlobFiles,
        limits: SpaceMediaLimits,
    ) -> None:
        self._repo = repo
        self._files = files
        self._limits = limits
        self._counters = _Counters()

    @property
    def limits(self) -> SpaceMediaLimits:
        return self._limits

    def admit_size(self, size: int) -> None:
        """Refuse (413 ``too_large_for_cache``, counted) a blob of *size*
        bytes this cache never takes — the route asks before it reads a
        body, with the signed size."""
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
        """Keep one blob for the scope. ``True`` when stored, ``False`` when
        it already was (a retry). Raises :class:`MediaRefused`."""
        if scope_kind not in MEDIA_SCOPES:
            raise MediaRefused(422, "bad_scope")
        now = int(time.time()) if now is None else now
        check_shape(blob_id, data)
        size = len(data)
        self.admit_size(size)
        if expires_at <= now:
            raise MediaRefused(422, "bad_expiry")
        expires_at = min(expires_at, now + self._limits.ttl_s)
        await check_address(blob_id, data)
        held = await self._repo.get(blob_id)
        if held is not None:
            if held.expires_at > now:
                if (held.scope_kind, held.scope_id) != (scope_kind, scope_id):
                    raise MediaRefused(409, "blob_id_taken")
                return False
            # Expired but not swept yet: replace it.
            await self._drop([held.blob_id])
        _, recent = await self._repo.scope_usage(
            scope_kind, scope_id, now=now, since=now - _RATE_WINDOW_S
        )
        if recent >= self._limits.space_uploads_per_hour:
            self._counters.refused_quota += 1
            raise MediaRefused(429, "space_media_quota")
        await self._make_room_in_scope(scope_kind, scope_id, size, now)
        await self._make_room(size, now)
        await self._files.write(blob_id, data)
        return await self._repo.add(
            GfsSpaceMedia(
                blob_id=blob_id,
                scope_kind=scope_kind,
                scope_id=scope_id,
                size=size,
                created_at=now,
                expires_at=expires_at,
                last_fetched_at=now,
            )
        )

    async def read(self, blob_id: str, *, now: int | None = None) -> bytes | None:
        """The blob, or ``None`` when unknown, expired, of a banned space or
        evicted. Marks the fetch at most once an hour."""
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
        """Withdraw a blob of the scope (its authority / channel key asked)."""
        if not is_blob_id(blob_id):
            return False
        if not await self._repo.delete(
            blob_id, scope_kind=scope_kind, scope_id=scope_id
        ):
            return False
        await self._files.remove(blob_id)
        return True

    async def prune_expired(self, now: int | None = None) -> int:
        """Drop expired blobs (rows and files); the maintenance sweep."""
        now = int(time.time()) if now is None else now
        dropped = 0
        for _ in range(PRUNE_MAX_BATCHES):
            ids = await self._repo.expired(now, limit=PRUNE_BATCH)
            await self._drop(ids)
            dropped += len(ids)
            if len(ids) < PRUNE_BATCH:
                break
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

    async def _make_room_in_scope(
        self, scope_kind: str, scope_id: str, size: int, now: int
    ) -> None:
        """Free the scope's own space until *size* more live bytes fit under
        ``space_max_bytes``: expired blobs go first (they count for
        nothing), then its least recently fetched, then oldest, blobs."""
        cap = self._limits.space_max_bytes
        live, _ = await self._repo.scope_usage(scope_kind, scope_id, now=now, since=now)
        if live + size <= cap:
            return
        await self.prune_expired(now)
        while live + size > cap:
            batch = await self._repo.least_recently_fetched(
                limit=EVICT_BATCH, scope=(scope_kind, scope_id)
            )
            if not batch:
                break
            victims: list[str] = []
            for media in batch:
                if live + size <= cap:
                    break
                victims.append(media.blob_id)
                live -= media.size
                self._count_eviction(media.size)
            await self._drop(victims)

    async def _make_room(self, size: int, now: int) -> None:
        """Free space until *size* more bytes fit under ``max_bytes``:
        expired blobs first, then least recently fetched, then oldest."""
        _, total = await self._repo.totals()
        if total + size <= self._limits.max_bytes:
            return
        await self.prune_expired(now)
        _, total = await self._repo.totals()
        while total + size > self._limits.max_bytes:
            batch = await self._repo.least_recently_fetched(limit=EVICT_BATCH)
            if not batch:
                break
            victims: list[str] = []
            for media in batch:
                if total + size <= self._limits.max_bytes:
                    break
                victims.append(media.blob_id)
                total -= media.size
                self._count_eviction(media.size)
            await self._drop(victims)
        log.debug("space media: evicted down to %d bytes held", total)

    def _count_eviction(self, size: int) -> None:
        self._counters.evicted_blobs += 1
        self._counters.evicted_bytes += size

    async def _drop(self, blob_ids: list[str]) -> None:
        """Files first, then rows: a crash in between leaves a row whose
        file is gone (a miss, swept at expiry), never an unlisted file."""
        for blob_id in blob_ids:
            await self._files.remove(blob_id)
        await self._repo.delete_ids(blob_ids)
