"""Space files for households reached only through a GFS — the reader side.

An item that reaches this household through a GFS (a member seated from an
invite link, a follower of a global space) carries a ``media_refs``
descriptor instead of the bytes (:mod:`socialhome.domain.space_media_ref`).
This service:

* **keeps** each entry for a file the item itself shows as a
  ``space_media_refs`` row (:meth:`SpaceMediaGfsFetcher.receive`);
* **prefetches** the pictures on arrival, at most
  :data:`PREFETCH_CONCURRENCY` at a time and :data:`MAX_PREFETCH_PENDING`
  waiting (the rest are fetched when viewed);
* **fetches on view** (:meth:`SpaceMediaGfsFetcher.ensure_local`, called by
  ``GET /api/media/{filename}`` for a missing file): concurrent requests for
  one file share one fetch, bounded by a timeout.

A fetch reads ``GET /gfs/media/{blob_id}`` (anonymous) from the GFS
this household reaches the space through — the link-joined pair's
``relay_via`` first, then its own GFS connections — checks that the blob
sits at its **scope-bound address** (``space_media_address`` with the scope
id THIS household knows: the space id, or its own channel id for a private
space; the descriptor names only the scope kind), decrypts with the epoch's media key
(:mod:`socialhome.space_media_seal`) and writes the file write-once under its
local name (the ``SPACE_MEDIA_BLOB`` rules, :mod:`.inbound_media_store`). An
unknown suite, a wrong hash or a failed decrypt is refused with a WARNING and
writes nothing.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterable
from typing import TYPE_CHECKING

import aiofiles
import aiofiles.os
from cryptography.exceptions import InvalidTag

from ..domain.federation import InstanceSource
from ..domain.gfs_space_media import SCOPE_CHANNEL, SCOPE_SPACE, space_media_address
from ..domain.space_media_ref import SpaceMediaRef, parse_media_refs
from ..space_media_seal import (
    UnsupportedSpaceMediaSuite,
    open_space_media,
    require_space_media_suite,
)
from .inbound_media_store import (
    is_reserved_media_name,
    media_file_path,
    write_once,
)
from .space_media_gfs_uploader import media_names

if TYPE_CHECKING:
    from ..domain.federation import GfsConnection
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
    from ..repositories.space_media_ref_repo import AbstractSpaceMediaRefRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from .gfs_connection_service import GfsConnectionService
    from .space_crypto_service import SpaceContentEncryption

log = logging.getLogger(__name__)

#: Prefetches running at once, waiting at most, and waiting at most for ONE
#: space (so one member's items can't fill the queue for every space).
PREFETCH_CONCURRENCY: int = 3
MAX_PREFETCH_PENDING: int = 200
MAX_PREFETCH_PER_SPACE: int = 50

#: GFS fetches running at once, views and prefetches together.
MAX_CONCURRENT_FETCHES: int = 6

#: How long a file no GFS held is not asked for again on a view (each ask
#: may try up to MAX_SOURCES GFSs).
MISS_TTL_S: float = 300.0
MAX_MISSES_REMEMBERED: int = 4096

#: How long a viewer waits for a fetch-on-view; one GFS request's timeout.
VIEW_TIMEOUT_S: float = 20.0
REQUEST_TIMEOUT_S: float = 30.0

#: GFSs tried per fetch.
MAX_SOURCES: int = 4

#: File extensions fetched on arrival (pictures); anything else on view.
PREFETCH_EXTENSIONS: frozenset[str] = frozenset({"webp", "jpg", "jpeg", "png", "gif"})

#: A prefetch that found the blob on no GFS tries again after these
#: delays: the item (and its descriptor) usually travels faster than the
#: author's upload of the file. Then it waits for a view.
PREFETCH_RETRY_DELAYS_S: tuple[float, ...] = (10.0, 60.0)

_PARTIAL_DIR = ".partial"

#: What one fetch came to.
_STORED = "stored"
_MISS = "miss"
_REFUSED = "refused"


def _monotonic() -> float:
    return time.monotonic()


class SpaceMediaGfsFetcher:
    """Keep ``media_refs`` and turn them into local files."""

    __slots__ = (
        "_crypto",
        "_dir",
        "_federation_repo",
        "_fetch_sem",
        "_gfs",
        "_gfs_repo",
        "_inflight",
        "_misses",
        "_prefetch_by_space",
        "_prefetch_pending",
        "_prefetch_sem",
        "_refs",
        "_spaces",
        "_tasks",
    )

    def __init__(
        self,
        *,
        refs_repo: AbstractSpaceMediaRefRepo,
        space_crypto: SpaceContentEncryption,
        gfs_service: GfsConnectionService,
        gfs_repo: AbstractGfsConnectionRepo,
        federation_repo: AbstractFederationRepo,
        space_repo: AbstractSpaceRepo,
        media_dir: pathlib.Path,
    ) -> None:
        self._refs = refs_repo
        self._crypto = space_crypto
        self._gfs = gfs_service
        self._gfs_repo = gfs_repo
        self._federation_repo = federation_repo
        #: Only for this household's own channel id of a private space.
        self._spaces = space_repo
        self._dir = pathlib.Path(media_dir)
        #: filename → the one fetch running for it (coalescing).
        self._inflight: dict[str, asyncio.Task[str]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._prefetch_sem = asyncio.Semaphore(PREFETCH_CONCURRENCY)
        self._prefetch_pending = 0
        #: Prefetches waiting per space (:data:`MAX_PREFETCH_PER_SPACE`).
        self._prefetch_by_space: dict[str, int] = {}
        #: Every GFS fetch, views and prefetches alike.
        self._fetch_sem = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
        #: filename → monotonic time until which a view doesn't ask again.
        self._misses: OrderedDict[str, float] = OrderedDict()

    # ── Descriptors ──────────────────────────────────────────────────────

    async def receive(
        self,
        space_id: str,
        raw_refs: object,
        media_urls: Iterable[str | None],
        *,
        item_id: str,
        origin: str = "",
        prefetch: bool = True,
    ) -> list[SpaceMediaRef]:
        """Keep the descriptor entries of item *item_id* of *space_id*
        (delivered by household *origin*) for the files it shows
        (*media_urls*); prefetch its pictures. Never raises.

        A file's row is bound to the item that introduced it and written
        once: a descriptor of an already described file from another item
        or space is refused with a WARNING (never re-points it), a
        redelivery of the same item is a no-op."""
        # Never a name another receiving path owns (DM / moment media).
        allowed = {
            name for name in media_names(media_urls) if not is_reserved_media_name(name)
        }
        parsed = parse_media_refs(
            raw_refs,
            space_id=space_id,
            allowed=allowed,
            item_id=item_id,
            origin=origin,
        )
        refs: list[SpaceMediaRef] = []
        try:
            for ref in parsed:
                held = await self._refs.get(ref.filename)
                if held is None:
                    refs.append(ref)
                elif (held.space_id, held.item_id) != (space_id, item_id):
                    log.warning(
                        "space media %s: described by another item or space "
                        "already — descriptor from %s refused",
                        ref.filename,
                        origin or "a relay",
                    )
            if refs:
                await self._refs.save_many(refs)
        except Exception:
            log.warning(
                "space media refs of space %s not stored", space_id, exc_info=True
            )
            return []
        if prefetch:
            for ref in refs:
                if _is_picture(ref.filename):
                    self._prefetch(ref.filename, space_id)
        return refs

    # ── Fetching ─────────────────────────────────────────────────────────

    async def ensure_local(
        self, filename: str, *, timeout_s: float = VIEW_TIMEOUT_S
    ) -> bool:
        """Whether *filename* is (now) a local file: fetched from a GFS when
        a ``space_media_refs`` row names it. Concurrent calls share one
        fetch; this caller waits at most *timeout_s* (the fetch goes on)."""
        path = media_file_path(self._dir, filename)
        if path is None:
            return False
        if await aiofiles.os.path.isfile(path):
            return True
        if self._misses.get(filename, 0.0) > _monotonic():
            return False  # no GFS held it a moment ago: don't ask them all again
        task = self._fetch_task(filename)
        try:
            outcome = await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
        except TimeoutError:
            log.warning("space media %s: fetch from the GFS timed out", filename)
            return False
        if outcome == _MISS:
            self._misses[filename] = _monotonic() + MISS_TTL_S
            self._misses.move_to_end(filename)
            while len(self._misses) > MAX_MISSES_REMEMBERED:
                self._misses.popitem(last=False)
        return outcome == _STORED

    def _fetch_task(self, filename: str) -> asyncio.Task[str]:
        task = self._inflight.get(filename)
        if task is None:
            task = asyncio.create_task(self._fetch_bounded(filename))
            self._inflight[filename] = task
            self._tasks.add(task)

            def _done(t: asyncio.Task, name: str = filename) -> None:
                self._inflight.pop(name, None)
                self._tasks.discard(t)

            task.add_done_callback(_done)
        return task

    async def _fetch_bounded(self, filename: str) -> str:
        async with self._fetch_sem:
            return await self._fetch(filename)

    def _prefetch(self, filename: str, space_id: str) -> None:
        if (
            self._prefetch_pending >= MAX_PREFETCH_PENDING
            or self._prefetch_by_space.get(space_id, 0) >= MAX_PREFETCH_PER_SPACE
        ):
            log.info(
                "space media prefetch backlog full — %s waits for a view", filename
            )
            return
        self._prefetch_pending += 1
        self._prefetch_by_space[space_id] = self._prefetch_by_space.get(space_id, 0) + 1
        task = asyncio.create_task(self._prefetch_one(filename, space_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _prefetch_one(self, filename: str, space_id: str) -> None:
        try:
            async with self._prefetch_sem:
                outcome = await self._fetch_task(filename)
            for delay in PREFETCH_RETRY_DELAYS_S:
                if outcome != _MISS:
                    break
                await asyncio.sleep(delay)
                async with self._prefetch_sem:
                    outcome = await self._fetch_task(filename)
        except Exception:  # pragma: no cover - defensive
            log.warning("space media prefetch of %s failed", filename, exc_info=True)
        finally:
            self._prefetch_pending -= 1
            left = self._prefetch_by_space.get(space_id, 1) - 1
            if left > 0:
                self._prefetch_by_space[space_id] = left
            else:
                self._prefetch_by_space.pop(space_id, None)

    async def drain(self) -> None:
        """Wait for every prefetch and fetch running now (tests, the demo)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def stop(self) -> None:
        """Let running fetches finish (bounded), then cancel the rest."""
        if not self._tasks:
            return
        _done, pending = await asyncio.wait(list(self._tasks), timeout=5.0)
        for task in pending:
            task.cancel()

    async def _fetch(self, filename: str) -> str:
        """One fetch: ``stored`` (the file is here now), ``miss`` (no GFS
        holds the blob — yet) or ``refused`` (anything else: unknown
        suite, no key, wrong hash, failed decrypt — not worth retrying)."""
        if is_reserved_media_name(filename):
            log.warning("space media %s: a reserved name — never fetched", filename)
            return _REFUSED
        ref = await self._refs.get(filename)
        path = media_file_path(self._dir, filename)
        if ref is None or path is None:
            return _REFUSED
        if await aiofiles.os.path.isfile(path):
            return _STORED
        try:
            require_space_media_suite(ref.suite)
        except UnsupportedSpaceMediaSuite as exc:
            log.warning("space media %s refused: %s", filename, exc)
            return _REFUSED
        current = await self._crypto.media_key(ref.space_id, epoch=ref.epoch)
        if current is None:
            log.warning(
                "space media %s: no key for space %s epoch %d — not fetched",
                filename,
                ref.space_id,
                ref.epoch,
            )
            return _REFUSED
        scope_id = await self._scope_id(ref)
        if scope_id is None:
            log.warning(
                "space media %s: no %s id known here for space %s — not fetched",
                filename,
                ref.scope,
                ref.space_id,
            )
            return _REFUSED
        outcome = _MISS
        for conn in await self._sources(ref.space_id):
            base_url = conn.inbox_url
            blob = await self._gfs.fetch_space_media(
                conn, ref.blob_id, max_bytes=ref.size, timeout_s=REQUEST_TIMEOUT_S
            )
            if blob is None:
                continue
            try:
                address = await asyncio.to_thread(
                    space_media_address,
                    ref.scope,
                    scope_id,
                    blob,
                    suite=ref.addr_suite,
                )
            except ValueError:
                address = ""
            if address != ref.blob_id:
                log.warning(
                    "space media %s from %s: not at its %s address — refused",
                    filename,
                    base_url,
                    ref.scope,
                )
                outcome = _REFUSED
                continue
            try:
                data = await asyncio.to_thread(
                    _open_sync, blob, current[1], ref.space_id, ref.epoch, filename
                )
            except ValueError, InvalidTag:
                log.warning(
                    "space media %s: cannot decrypt the blob — refused", filename
                )
                return _REFUSED
            await self._write(path, data)
            log.info(
                "space media %s: fetched blob %s (%d bytes) from GFS %s",
                filename,
                ref.blob_id[:12],
                len(blob),
                base_url,
            )
            return _STORED
        if outcome == _MISS:
            log.info(
                "space media %s: no GFS holds blob %s (yet)", filename, ref.blob_id[:12]
            )
        return outcome

    async def _scope_id(self, ref: SpaceMediaRef) -> str | None:
        """The scope id this household itself knows for *ref*'s scope: the
        space id, or the opaque channel id of its own grant — never one a
        descriptor or a GFS supplies."""
        if ref.scope == SCOPE_SPACE:
            return ref.space_id
        if ref.scope == SCOPE_CHANNEL:
            stored = await self._spaces.get_gfs_channel(ref.space_id)
            return stored[0] if stored is not None else None
        return None

    async def _sources(self, space_id: str) -> list[GfsConnection]:
        """The GFS connections to ask, in order: this household's connection
        to the GFS each link-joined pair of this space relays through, then
        its other connections. Only connections — a fetch rides the
        cookie-less, address-pinned session of one; a ``relay_via`` URL this
        household holds no connection to is never fetched (review E)."""
        conns: list[GfsConnection] = []
        seen: set[str] = set()
        for iid in await self._federation_repo.list_member_instance_ids(space_id):
            inst = await self._federation_repo.get_instance(iid)
            if (
                inst is None
                or inst.source is not InstanceSource.SPACE_SESSION
                or not inst.relay_via
            ):
                continue
            conn = await self._gfs.connection_for_url(inst.relay_via)
            if conn is not None and conn.id not in seen:
                seen.add(conn.id)
                conns.append(conn)
        for conn in await self._gfs_repo.list_active():
            if conn.inbox_url and conn.id not in seen:
                seen.add(conn.id)
                conns.append(conn)
        return conns[:MAX_SOURCES]

    async def _write(self, path: pathlib.Path, data: bytes) -> None:
        await write_once(
            path, data, self._dir / _PARTIAL_DIR / f"gfs-{uuid.uuid4().hex}"
        )


def _is_picture(filename: str) -> bool:
    return filename.rpartition(".")[2].lower() in PREFETCH_EXTENSIONS


def _open_sync(
    blob: bytes, key: bytes, space_id: str, epoch: int, filename: str
) -> bytes:
    return open_space_media(
        blob=blob, media_key=key, space_id=space_id, epoch=epoch, filename=filename
    )
