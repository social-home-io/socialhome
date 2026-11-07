"""Space files for households reached only through a GFS — the reader side.

An item that reaches this household through a GFS (a member seated from an
invite link, a follower of a global space) carries a ``media_refs``
descriptor instead of the bytes (:mod:`socialhome.domain.space_media_ref`).
This service:

* **localizes** the item before it is saved
  (:meth:`SpaceMediaGfsFetcher.localize`): each file the item shows and
  describes is renamed to THIS household's name for it,
  :func:`~.inbound_media_store.gfs_space_media_name` of the space, the
  household that delivered the item (authenticated by the caller — the
  envelope, the signed inner, the §25.6 stream) and the author's file name.
  So a member naming another item's file in its own item only ever gets
  its own copy; it can never take over the name another item shows. A file
  the same item already has here keeps the name it has (an update without
  a descriptor, a second household re-describing it);
* **keeps** the new entries as ``space_media_refs`` rows once the item is
  stored (:meth:`SpaceMediaGfsFetcher.keep`);
* **prefetches** the pictures on arrival, at most
  :data:`PREFETCH_CONCURRENCY` at a time, :data:`MAX_PREFETCH_PENDING`
  waiting and :data:`MAX_PREFETCH_PER_SPACE` of one space (the rest are
  fetched when viewed); a space whose last :data:`MAX_SPACE_PREFETCH_MISSES`
  prefetches found nothing tries once, without retries;
* **fetches on view** (:meth:`SpaceMediaGfsFetcher.ensure_local`, called by
  ``GET /api/media/{filename}`` for a missing file): concurrent requests for
  one file share one fetch, bounded by a timeout.

A space page's files (v_81) are the exception to the reader name: every
household keeps the host's version of a page byte for byte (its hash is
the version's identity), so a page's file keeps the name the page shows
(:meth:`SpaceMediaGfsFetcher.localize_as_shown`, only for the host's
version) and is fetched under it only while a page of that space, the item
the row is bound to, still shows it (:meth:`attach_pages`).

A fetch reads ``GET /gfs/media/{blob_id}`` (anonymous) from the GFS
this household reaches the space through — the link-joined pair's
``relay_via`` first, then its own GFS connections — checks that the blob
sits at its **scope-bound address** (``space_media_address`` with the scope
id THIS household knows: the space id, or its own channel id for a private
space; the descriptor names only the scope kind), decrypts with the epoch's media key — the AAD names the item and the
delivering household (:mod:`socialhome.space_media_seal`) — and writes the
file write-once under its local name (:mod:`.inbound_media_store`). An
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
from dataclasses import replace
from typing import TYPE_CHECKING

import aiofiles
import aiofiles.os
from cryptography.exceptions import InvalidTag

from ..domain.federation import InstanceSource
from ..domain.gfs_space_media import SCOPE_CHANNEL, SCOPE_SPACE, space_media_address
from ..domain.space_media_ref import (
    SpaceMediaLocalization,
    SpaceMediaRef,
    page_media_urls,
    parse_media_refs,
)
from ..space_media_seal import (
    UnsupportedSpaceMediaSuite,
    open_space_media,
    require_space_media_suite,
)
from .inbound_media_store import (
    gfs_space_media_name,
    is_gfs_space_media_name,
    is_reserved_media_name,
    media_file_path,
    write_once,
)
from .space_media_gfs_uploader import media_names

if TYPE_CHECKING:
    from ..domain.federation import GfsConnection
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
    from ..repositories.page_repo import AbstractPageRepo
    from ..repositories.space_media_ref_repo import AbstractSpaceMediaRefRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from .gfs_connection_service import GfsConnectionService
    from .space_crypto_service import SpaceContentEncryption

log = logging.getLogger(__name__)

#: Prefetches running at once, waiting at most, and waiting at most for ONE
#: space (so one member's items can't fill the queue for every space).
PREFETCH_CONCURRENCY: int = 3
MAX_PREFETCH_PENDING: int = 200
MAX_PREFETCH_PER_SPACE: int = 20

#: After this many prefetches of one space in a row found their blob on no
#: GFS, that space's prefetches try once without retries — a member's items
#: naming blobs nobody uploads cost one request each, not three — until one
#: lands. Views fetch as always.
MAX_SPACE_PREFETCH_MISSES: int = 5

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
        "_pages",
        "_prefetch_by_space",
        "_prefetch_pending",
        "_prefetch_sem",
        "_refs",
        "_space_misses",
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
        #: local name → monotonic time until which a view doesn't ask again.
        self._misses: OrderedDict[str, float] = OrderedDict()
        #: space → prefetches in a row that found their blob nowhere.
        self._space_misses: OrderedDict[str, int] = OrderedDict()
        #: v_81 — a page file kept under the name its page shows.
        self._pages: AbstractPageRepo | None = None

    def attach_pages(self, page_repo: AbstractPageRepo) -> None:
        """Wire the space pages (v_81): a page's file keeps the name the
        page shows, and is fetched only while that page shows it."""
        self._pages = page_repo

    # ── Descriptors ──────────────────────────────────────────────────────

    async def localize(
        self,
        space_id: str,
        raw_refs: object,
        media_urls: Iterable[str | None],
        *,
        item_id: str,
        origin: str,
    ) -> SpaceMediaLocalization:
        """This household's names for the files item *item_id* of
        *space_id* shows (*media_urls*), as delivered by household *origin*
        — which the CALLER authenticated (the envelope's sender, the signed
        inner's origin, the §25.6 provider), never a descriptor field.

        * A described file the item has no row for yet → its reader name
          (:func:`gfs_space_media_name` of space, *origin*, file name) and a
          row to :meth:`keep` once the item is saved.
        * A file the item already has a row for → the name it has (the
          first household to describe it for this item keeps it; the same
          household's newer epoch moves the row in :meth:`keep`).
        * Anything else keeps its reference.

        Call it BEFORE saving the item, apply :meth:`SpaceMediaLocalization
        .url` / ``.post`` to it, and :meth:`keep` only when the item was
        stored. Never raises (an empty localization on failure)."""
        empty = SpaceMediaLocalization()
        if not item_id or not origin:
            return empty
        # Never a name another receiving path owns (DM / moment / GFS media).
        allowed = {
            name for name in media_names(media_urls) if not is_reserved_media_name(name)
        }
        if not allowed:
            return empty
        try:
            held: dict[str, SpaceMediaRef] = {}
            for row in await self._refs.list_for_item(space_id, item_id):
                held.setdefault(row.filename, row)
            names: dict[str, str] = {}
            refs: list[SpaceMediaRef] = []
            for ref in parse_media_refs(
                raw_refs,
                space_id=space_id,
                allowed=allowed,
                item_id=item_id,
                origin=origin,
            ):
                local = gfs_space_media_name(space_id, origin, item_id, ref.filename)
                first = held.get(ref.filename)
                if first is not None and first.local_name != local:
                    log.info(
                        "space media %s of %s: described by %s already — "
                        "the descriptor from %s keeps that",
                        ref.filename,
                        item_id,
                        first.origin,
                        origin,
                    )
                    continue
                names[ref.filename] = local
                refs.append(replace(ref, local_name=local))
            for name, row in held.items():
                if name in allowed:
                    names.setdefault(name, row.local_name)
        except Exception:
            log.warning(
                "space media refs of item %s in space %s not read",
                item_id,
                space_id,
                exc_info=True,
            )
            return empty
        return SpaceMediaLocalization(names=names, refs=tuple(refs))

    def localize_as_shown(
        self,
        space_id: str,
        raw_refs: object,
        media_urls: Iterable[str | None],
        *,
        item_id: str,
        origin: str,
    ) -> SpaceMediaLocalization:
        """v_81 — the rows for a space page's files, each kept under the
        name the page shows (``local_name`` = the file name; the page is
        never rewritten). Only for the host's version of page *item_id*:
        the CALLER authenticated *origin* as the space's host. A reserved
        name is never kept; a row is bound to the page (write-once, like
        any row). Never raises."""
        if not item_id or not origin or self._pages is None:
            return SpaceMediaLocalization()
        allowed = {
            name for name in media_names(media_urls) if not is_reserved_media_name(name)
        }
        if not allowed:
            return SpaceMediaLocalization()
        try:
            refs = tuple(
                replace(ref, local_name=ref.filename)
                for ref in parse_media_refs(
                    raw_refs,
                    space_id=space_id,
                    allowed=allowed,
                    item_id=item_id,
                    origin=origin,
                )
            )
        except Exception:
            log.warning(
                "space media refs of page %s in space %s not read",
                item_id,
                space_id,
                exc_info=True,
            )
            return SpaceMediaLocalization()
        return SpaceMediaLocalization(refs=refs)

    async def _shown_by_its_page(self, ref: SpaceMediaRef) -> bool:
        """Whether *ref* (a row kept under its own file name) belongs to a
        live page of its space that shows that file."""
        if self._pages is None or ref.local_name != ref.filename:
            return False
        page = await self._pages.get_space_page(ref.item_id, space_id=ref.space_id)
        if page is None:
            return False
        return ref.filename in media_names(
            page_media_urls(page.cover_image_url, page.content)
        )

    async def has_item_refs(self, space_id: str, item_id: str) -> bool:
        """Whether item *item_id* of *space_id* has any ``space_media_refs``
        row here (the follower heal is add-only: it never touches an item
        that has). ``True`` on a read failure — never heal blind."""
        try:
            return bool(await self._refs.list_for_item(space_id, item_id))
        except Exception:
            log.warning("space media refs of item %s not read", item_id, exc_info=True)
            return True

    async def keep(
        self, loc: SpaceMediaLocalization, *, prefetch: bool = True
    ) -> list[SpaceMediaRef]:
        """Keep *loc*'s new rows — call it once the item is stored — and
        prefetch their pictures. Never raises."""
        if not loc.refs:
            return []
        try:
            await self._refs.save_many(loc.refs)
        except Exception:
            log.warning("space media refs not stored", exc_info=True)
            return []
        for ref in loc.refs:
            # A newer epoch may have moved the row: ask the GFS again.
            self._misses.pop(ref.local_name, None)
            if prefetch and _is_picture(ref.local_name):
                self._prefetch(ref.local_name, ref.space_id)
        return list(loc.refs)

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
        if not is_gfs_space_media_name(filename) and (
            self._pages is None
            or is_reserved_media_name(filename)
            or await self._refs.get(filename) is None
        ):
            # A file another household pushes (or a local one) that is not
            # here yet: routine for a viewer, never a GFS fetch — unless a
            # page's row names it (v_81, checked in :meth:`_fetch`).
            return False
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
            if self._space_misses.get(space_id, 0) < MAX_SPACE_PREFETCH_MISSES:
                for delay in PREFETCH_RETRY_DELAYS_S:
                    if outcome != _MISS:
                        break
                    await asyncio.sleep(delay)
                    async with self._prefetch_sem:
                        outcome = await self._fetch_task(filename)
            self._count_space_miss(space_id, outcome)
        except Exception:  # pragma: no cover - defensive
            log.warning("space media prefetch of %s failed", filename, exc_info=True)
        finally:
            self._prefetch_pending -= 1
            left = self._prefetch_by_space.get(space_id, 1) - 1
            if left > 0:
                self._prefetch_by_space[space_id] = left
            else:
                self._prefetch_by_space.pop(space_id, None)

    def _count_space_miss(self, space_id: str, outcome: str) -> None:
        if outcome == _STORED:
            self._space_misses.pop(space_id, None)
        elif outcome == _MISS:
            self._space_misses[space_id] = self._space_misses.get(space_id, 0) + 1
            self._space_misses.move_to_end(space_id)
            while len(self._space_misses) > MAX_MISSES_REMEMBERED:
                self._space_misses.popitem(last=False)

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
        ref = await self._refs.get(filename)
        path = media_file_path(self._dir, filename)
        if ref is None or path is None:
            return _REFUSED
        if not is_gfs_space_media_name(filename) and (
            is_reserved_media_name(filename) or not await self._shown_by_its_page(ref)
        ):
            log.warning(
                "space media %s: neither a GFS space media name nor a file its "
                "page shows — never fetched",
                filename,
            )
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
                data = await asyncio.to_thread(_open_sync, blob, current[1], ref)
            except ValueError, InvalidTag:
                log.warning(
                    "space media %s: cannot decrypt the blob — refused", filename
                )
                return _REFUSED
            await self._write(path, data)
            log.info(
                "space media %s (%s of %s): fetched blob %s (%d bytes) from GFS %s",
                filename,
                ref.filename,
                ref.item_id,
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


def _open_sync(blob: bytes, key: bytes, ref: SpaceMediaRef) -> bytes:
    return open_space_media(
        blob=blob,
        media_key=key,
        space_id=ref.space_id,
        epoch=ref.epoch,
        item_id=ref.item_id,
        origin=ref.origin,
        filename=ref.filename,
    )
