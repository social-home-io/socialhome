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

A fetch reads ``GET /gfs/spaces/media/{blob_id}`` (anonymous) from the GFS
this household reaches the space through — the link-joined pair's
``relay_via`` first, then its own GFS connections — checks the SHA-256
against ``blob_id``, decrypts with the epoch's media key
(:mod:`socialhome.space_media_seal`) and writes the file write-once under its
local name (the ``SPACE_MEDIA_BLOB`` rules, :mod:`.inbound_media_store`). An
unknown suite, a wrong hash or a failed decrypt is refused with a WARNING and
writes nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import pathlib
import uuid
from collections.abc import Iterable
from typing import TYPE_CHECKING

import aiofiles
import aiofiles.os
from cryptography.exceptions import InvalidTag

from ..domain.federation import InstanceSource
from ..domain.space_media_ref import SpaceMediaRef, parse_media_refs
from ..space_media_seal import (
    UnsupportedSpaceMediaSuite,
    open_space_media,
    require_space_media_suite,
)
from .inbound_media_store import media_file_path, publish_once, remove_quietly
from .space_media_gfs_uploader import media_names

if TYPE_CHECKING:
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
    from ..repositories.space_media_ref_repo import AbstractSpaceMediaRefRepo
    from .gfs_connection_service import GfsConnectionService
    from .space_crypto_service import SpaceContentEncryption

log = logging.getLogger(__name__)

#: Prefetches running at once, and waiting at most.
PREFETCH_CONCURRENCY: int = 3
MAX_PREFETCH_PENDING: int = 200

#: How long a viewer waits for a fetch-on-view; one GFS request's timeout.
VIEW_TIMEOUT_S: float = 20.0
REQUEST_TIMEOUT_S: float = 30.0

#: GFSs tried per fetch.
MAX_SOURCES: int = 4

#: File extensions fetched on arrival (pictures); anything else on view.
PREFETCH_EXTENSIONS: frozenset[str] = frozenset({"webp", "jpg", "jpeg", "png", "gif"})

_PARTIAL_DIR = ".partial"


class SpaceMediaGfsFetcher:
    """Keep ``media_refs`` and turn them into local files."""

    __slots__ = (
        "_crypto",
        "_dir",
        "_federation_repo",
        "_gfs",
        "_gfs_repo",
        "_inflight",
        "_prefetch_pending",
        "_prefetch_sem",
        "_refs",
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
        media_dir: pathlib.Path,
    ) -> None:
        self._refs = refs_repo
        self._crypto = space_crypto
        self._gfs = gfs_service
        self._gfs_repo = gfs_repo
        self._federation_repo = federation_repo
        self._dir = pathlib.Path(media_dir)
        #: filename → the one fetch running for it (coalescing).
        self._inflight: dict[str, asyncio.Task[bool]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._prefetch_sem = asyncio.Semaphore(PREFETCH_CONCURRENCY)
        self._prefetch_pending = 0

    # ── Descriptors ──────────────────────────────────────────────────────

    async def receive(
        self,
        space_id: str,
        raw_refs: object,
        media_urls: Iterable[str | None],
        *,
        prefetch: bool = True,
    ) -> list[SpaceMediaRef]:
        """Keep the descriptor entries of an item of *space_id* that shows
        *media_urls*; prefetch its pictures. Never raises."""
        allowed = set(media_names(media_urls))
        refs = parse_media_refs(raw_refs, space_id=space_id, allowed=allowed)
        if not refs:
            return []
        try:
            await self._refs.save_many(refs)
        except Exception:
            log.warning(
                "space media refs of space %s not stored", space_id, exc_info=True
            )
            return []
        if prefetch:
            for ref in refs:
                if _is_picture(ref.filename):
                    self._prefetch(ref.filename)
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
        task = self._fetch_task(filename)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
        except TimeoutError:
            log.warning("space media %s: fetch from the GFS timed out", filename)
            return False

    def _fetch_task(self, filename: str) -> asyncio.Task[bool]:
        task = self._inflight.get(filename)
        if task is None:
            task = asyncio.create_task(self._fetch(filename))
            self._inflight[filename] = task
            self._tasks.add(task)

            def _done(t: asyncio.Task, name: str = filename) -> None:
                self._inflight.pop(name, None)
                self._tasks.discard(t)

            task.add_done_callback(_done)
        return task

    def _prefetch(self, filename: str) -> None:
        if self._prefetch_pending >= MAX_PREFETCH_PENDING:
            log.info(
                "space media prefetch backlog full — %s waits for a view", filename
            )
            return
        self._prefetch_pending += 1
        task = asyncio.create_task(self._prefetch_one(filename))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _prefetch_one(self, filename: str) -> None:
        try:
            async with self._prefetch_sem:
                await self._fetch_task(filename)
        except Exception:  # pragma: no cover - defensive
            log.warning("space media prefetch of %s failed", filename, exc_info=True)
        finally:
            self._prefetch_pending -= 1

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

    async def _fetch(self, filename: str) -> bool:
        ref = await self._refs.get(filename)
        path = media_file_path(self._dir, filename)
        if ref is None or path is None:
            return False
        if await aiofiles.os.path.isfile(path):
            return True
        try:
            require_space_media_suite(ref.suite)
        except UnsupportedSpaceMediaSuite as exc:
            log.warning("space media %s refused: %s", filename, exc)
            return False
        current = await self._crypto.media_key(ref.space_id, epoch=ref.epoch)
        if current is None:
            log.warning(
                "space media %s: no key for space %s epoch %d — not fetched",
                filename,
                ref.space_id,
                ref.epoch,
            )
            return False
        for base_url in await self._sources(ref.space_id):
            blob = await self._gfs.fetch_space_media(
                base_url, ref.blob_id, max_bytes=ref.size, timeout_s=REQUEST_TIMEOUT_S
            )
            if blob is None:
                continue
            if hashlib.sha256(blob).hexdigest() != ref.blob_id:
                log.warning(
                    "space media %s from %s: hash does not match — refused",
                    filename,
                    base_url,
                )
                continue
            try:
                data = await asyncio.to_thread(
                    _open_sync, blob, current[1], ref.space_id, ref.epoch, filename
                )
            except ValueError, InvalidTag:
                log.warning(
                    "space media %s: cannot decrypt the blob — refused", filename
                )
                return False
            await self._write(path, data)
            log.info(
                "space media %s: fetched blob %s (%d bytes) from GFS %s",
                filename,
                ref.blob_id[:12],
                len(blob),
                base_url,
            )
            return True
        log.info("space media %s: no GFS holds blob %s", filename, ref.blob_id[:12])
        return False

    async def _sources(self, space_id: str) -> list[str]:
        """The GFS base URLs to ask, in order: the GFS each link-joined pair
        of this space relays through, then this household's connections."""
        urls: list[str] = []
        for iid in await self._federation_repo.list_member_instance_ids(space_id):
            inst = await self._federation_repo.get_instance(iid)
            if (
                inst is not None
                and inst.source is InstanceSource.SPACE_SESSION
                and inst.relay_via
                and inst.relay_via not in urls
            ):
                urls.append(inst.relay_via)
        for conn in await self._gfs_repo.list_active():
            if conn.inbox_url and conn.inbox_url not in urls:
                urls.append(conn.inbox_url)
        return urls[:MAX_SOURCES]

    async def _write(self, path: pathlib.Path, data: bytes) -> None:
        partial = self._dir / _PARTIAL_DIR
        await aiofiles.os.makedirs(partial, exist_ok=True)
        tmp = partial / f"gfs-{uuid.uuid4().hex}"
        try:
            async with aiofiles.open(tmp, "wb") as fh:
                await fh.write(data)
            await publish_once(tmp, path)
        finally:
            await remove_quietly(tmp)


def _is_picture(filename: str) -> bool:
    return filename.rpartition(".")[2].lower() in PREFETCH_EXTENSIONS


def _open_sync(
    blob: bytes, key: bytes, space_id: str, epoch: int, filename: str
) -> bytes:
    return open_space_media(
        blob=blob, media_key=key, space_id=space_id, epoch=epoch, filename=filename
    )
