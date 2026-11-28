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
  the item's valid v_85 hash map covers takes its content name
  (``space_content_media_name``) instead — the one name it has on every
  path, so a host's or helper's push lands where the row points — and its
  fetched bytes must hash to it. A file the same item already has here
  keeps the name it has (an update without a descriptor, a second household
  re-describing it);
* **keeps** the new entries as ``space_media_refs`` rows once the item is
  stored (:meth:`SpaceMediaGfsFetcher.keep`);
* **syncs** the files the per-source policy keeps without a view
  (:mod:`socialhome.domain.space_media_sync`, owner 2026-11-28: a live
  item's files, galleries and current pages always, new posts and
  comments, upcoming events, active listings — each at most 50 MiB), at
  most :data:`PREFETCH_CONCURRENCY` at a time (a catch-up's at most one, so
  live content goes first), :data:`MAX_PREFETCH_PENDING` files and
  :data:`MAX_PREFETCH_BYTES` waiting and :data:`MAX_PREFETCH_PER_SPACE` of
  one space (the rest are fetched when viewed); a space whose last
  :data:`MAX_SPACE_PREFETCH_MISSES` prefetches found nothing tries once,
  without retries. A space's synced files are bounded by the household's
  storage setting (:meth:`attach_sync_budget`, default 2 GiB per space, 0 =
  only what it opens): :meth:`sweep` deletes the least recently viewed
  first (``space_media_refs.viewed_at``, migration 0159) — never one viewed
  within 30 days, one this household delivered, or anything on a household
  acting as the space's host; the row stays, so a view fetches it again;
* **archives** on the acting host of a GFS space (v_97, owner 2026-11-28):
  the household that owns the space (or a delegated admin household that
  acts as its host) fetches EVERY described file on arrival — videos and
  files in pieces too, the one exception to "big files are never
  prefetched" — so it stays the space's long-term holder once the GFS
  cache let the blobs go. A bounded background queue
  (:data:`HOST_ARCHIVE_CONCURRENCY` at a time, at most
  :data:`MAX_HOST_ARCHIVE_PENDING` files and :data:`MAX_HOST_ARCHIVE_BYTES`
  waiting); a file no GFS holds yet is asked for again after
  :data:`HOST_ARCHIVE_RETRY_DELAYS_S`, then a WARNING. A file past the
  bounds, or still queued at a restart, is fetched when someone views it;
* **fetches on view** (:meth:`SpaceMediaGfsFetcher.ensure_local`, called by
  ``GET /api/media/{filename}`` for a missing file): concurrent requests for
  one file share one fetch, bounded by a timeout.

A space page's files (v_82) are the exception to the reader name: every
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

**On demand (v_83).** A VIEW whose fetch found the blob on no GFS asks for
it once: ``POST /gfs/media/{blob_id}/wanted`` to the first source whose
signed ``space_media`` capability proves ``on_demand``, with the scope id
this household knows and a hint sealed for space key holders
(:mod:`socialhome.space_media_hint`: the item, the sealing household and
the file name — nothing about this household). The GFS has a holder
re-upload or stream the same ciphertext, which is checked and stored exactly
like a fetched blob; a miss there is a miss (the view's miss cache applies).
A prefetch never asks — only a person looking at the item makes a holder
send anything.

**Files in pieces (v_97, ``sm1p``).** A file over one blob is fetched piece
by piece — each piece's address derived from the file's, opened under its
piece name (a piece another key holder sealed for another place does not
open) and kept in a part directory, so a fetch cut short resumes with the
pieces still missing (a piece no GFS holds is asked for on demand, per
piece). Once every piece is here, the whole file must hash to the checksum
its descriptor carries — its content name (``sc-``) — or every piece is
dropped and nothing is written. A file in pieces is never prefetched. Since v_92 a household holding a member seat asks its host or
a trusted helper with ``SPACE_MEDIA_WANTED`` for an ``sc-`` content-named
file instead; it keeps this ask for a file it holds under any other name
(an ``sg-`` name from the public GFS copy — that request carries content
names only) and while the space's host is below v_92 (rollout: that host
answers no ``SPACE_MEDIA_WANTED``).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import pathlib
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

import aiofiles
import aiofiles.os
from cryptography.exceptions import InvalidTag

from ..domain.federation import InstanceSource
from ..domain.federation_capabilities import FederationCapability
from ..domain.file_hashes import (
    FileHash,
    is_space_content_media_name,
    sha256_hex,
    space_content_media_name,
)
from ..domain.gfs_space_media import (
    PIECE_BYTES,
    SCOPE_CHANNEL,
    piece_address,
    piece_count,
    space_media_address,
)
from ..domain.media_constraints import is_gfs_space_media_name, is_reserved_media_name
from ..domain.space_media_ref import (
    MAX_PAGE_DESCRIPTOR_ENTRIES,
    MAX_PAGE_FILES,
    SpaceMediaLocalization,
    SpaceMediaRef,
    page_media_urls,
    parse_media_refs,
    space_page_media_name,
)
from ..domain.space_media_sync import (
    DEFAULT_SPACE_SYNC_BYTES,
    LIVE_HINT,
    VIEWED_KEEP,
    SyncHint,
    SyncSource,
    parse_moment,
    syncs,
)
from ..federation.host_equivalence import serves_as_host
from ..moment_public_media import MAX_BLOB_BYTES
from ..space_media_hint import SpaceMediaLocator, seal_space_media_hint
from ..space_media_seal import (
    UnsupportedSpaceMediaSuite,
    open_space_media,
    piece_aad_name,
    require_space_media_suite,
)
from ..task_drain import wait_tasks_done
from .gfs_channel_service import channel_on
from .inbound_media_store import (
    content_name_binds,
    drop_parts,
    gfs_space_media_name,
    is_space_direct_media_name,
    is_space_page_media_name,
    media_file_path,
    part_name,
    present_parts,
    publish_once,
    remove_quietly,
    space_direct_media_name,
    write_once,
)
from .space_media_gfs_uploader import media_names
from .space_writer_households import holds_seat

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
MAX_PREFETCH_PENDING: int = 1000
MAX_PREFETCH_PER_SPACE: int = 200
#: Bytes the synced files waiting may add up to (owner 2026-11-28: files up
#: to 50 MiB are synced, not only pictures).
MAX_PREFETCH_BYTES: int = 1024 * 1024 * 1024
#: A catch-up's syncs (old records: a gallery, upcoming events) run at most
#: this many at once — live content goes first.
CATCH_UP_SYNC_CONCURRENCY: int = 1

#: A space's synced files are checked against the budget at most this often.
SWEEP_INTERVAL_S: float = 600.0
#: A view is written to ``space_media_refs.viewed_at`` at most this often
#: per file (one write a day, not one per request).
VIEW_STAMP_INTERVAL_S: float = 86_400.0
MAX_VIEW_STAMPS_REMEMBERED: int = 16_384

#: After this many prefetches of one space in a row found their blob on no
#: GFS, that space's prefetches try once without retries — a member's items
#: naming blobs nobody uploads cost one request each, not three — until one
#: lands. Views fetch as always.
MAX_SPACE_PREFETCH_MISSES: int = 5

#: v_97 (owner 2026-11-28) — the acting host of a GFS space archives every
#: described file: this many fetches at once, at most this many files and
#: bytes waiting (past them a file waits for a view, with a WARNING), and a
#: file no GFS holds yet (its upload — a long video's pieces — still
#: running) asked for again after these delays.
HOST_ARCHIVE_CONCURRENCY: int = 2
MAX_HOST_ARCHIVE_PENDING: int = 4096
MAX_HOST_ARCHIVE_BYTES: int = 4 * 1024 * 1024 * 1024
HOST_ARCHIVE_RETRY_DELAYS_S: tuple[float, ...] = (30.0, 300.0, 1800.0)

#: GFS fetches running at once, views and prefetches together.
MAX_CONCURRENT_FETCHES: int = 6

#: How long a file no GFS held is not asked for again on a view (each ask
#: may try up to MAX_SOURCES GFSs).
MISS_TTL_S: float = 300.0
MAX_MISSES_REMEMBERED: int = 4096

#: How long a viewer waits for a fetch-on-view; one GFS request's timeout.
VIEW_TIMEOUT_S: float = 20.0
REQUEST_TIMEOUT_S: float = 30.0

#: One 8 MiB piece's GET (v_97): a full piece over a ~1 Mbit/s link.
PIECE_TIMEOUT_S: float = 120.0

#: Bytes the part directories of files in pieces may hold together (v_97):
#: past it the least recently written ones go first (a fetch that never
#: completed), never the file being fetched.
MAX_PARTIAL_PIECE_BYTES: int = 512 * 1024 * 1024

#: GFSs tried per fetch.
MAX_SOURCES: int = 4

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


class _MemberVersions(Protocol):
    own_instance_id: str

    """The federation service's version answer (v_92), and whether this
    household reaches a space's host other than through a GFS (P10: the
    media fetch path is decided by path, never by role)."""

    async def reaches_host_directly(self, space_id: str) -> bool: ...

    async def space_member_supports(
        self, instance_id: str, *, min_version: int
    ) -> bool: ...


class _ChannelRing(Protocol):
    async def ring_connections(
        self, space_id: str
    ) -> tuple[str, list[GfsConnection]] | None: ...


class SpaceMediaGfsFetcher:
    """Keep ``media_refs`` and turn them into local files."""

    __slots__ = (
        "_active_parts",
        "_archive_bytes",
        "_archive_queue",
        "_archive_workers",
        "_budget",
        "_catch_up_sem",
        "_channels",
        "_crypto",
        "_dir",
        "_federation",
        "_federation_repo",
        "_fetch_sem",
        "_gfs",
        "_gfs_repo",
        "_inflight",
        "_misses",
        "_pages",
        "_prefetch_by_space",
        "_prefetch_bytes",
        "_prefetch_pending",
        "_prefetch_sem",
        "_refs",
        "_space_misses",
        "_spaces",
        "_swept",
        "_tasks",
        "_viewed",
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
        federation: _MemberVersions,
        media_dir: pathlib.Path,
    ) -> None:
        self._refs = refs_repo
        self._crypto = space_crypto
        self._gfs = gfs_service
        self._gfs_repo = gfs_repo
        self._federation_repo = federation_repo
        #: This household's own channel id of a private space, and whether
        #: it holds a member seat (only a follower asks its GFS, v_92).
        self._spaces = space_repo
        #: Whether the space's host answers ``SPACE_MEDIA_WANTED`` (v_92).
        self._federation = federation
        self._dir = pathlib.Path(media_dir)
        #: (filename, asks on a miss) → the one fetch running for it.
        self._inflight: dict[tuple[str, bool], asyncio.Task[str]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._prefetch_sem = asyncio.Semaphore(PREFETCH_CONCURRENCY)
        self._prefetch_pending = 0
        self._prefetch_bytes = 0
        self._catch_up_sem = asyncio.Semaphore(CATCH_UP_SYNC_CONCURRENCY)
        #: The household's storage for one space's synced files.
        self._budget: Callable[[], int] = lambda: DEFAULT_SPACE_SYNC_BYTES
        #: space → monotonic time of its last budget sweep.
        self._swept: dict[str, float] = {}
        #: local name → monotonic time its view was last stamped.
        self._viewed: OrderedDict[str, float] = OrderedDict()
        #: Prefetches waiting per space (:data:`MAX_PREFETCH_PER_SPACE`).
        self._prefetch_by_space: dict[str, int] = {}
        #: Every GFS fetch, views and prefetches alike.
        self._fetch_sem = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
        #: local name → monotonic time until which a view doesn't ask again.
        self._misses: OrderedDict[str, float] = OrderedDict()
        #: space → prefetches in a row that found their blob nowhere.
        self._space_misses: OrderedDict[str, int] = OrderedDict()
        #: v_82 — a page's files, kept under page-scoped ``sp-`` names.
        self._pages: AbstractPageRepo | None = None
        #: v_97 — the space's channel connections (tried first).
        self._channels: _ChannelRing | None = None
        #: v_97 — part directories of the pieced fetches running now (never
        #: trimmed by the part cap).
        self._active_parts: set[pathlib.Path] = set()
        #: v_97 — the acting host's archive queue: (local name, size,
        #: attempts so far), the bytes waiting in it and its workers.
        self._archive_queue: deque[tuple[str, int, int]] = deque()
        self._archive_bytes = 0
        self._archive_workers = 0

    def attach_sync_budget(self, budget: Callable[[], int]) -> None:
        """The household's storage for one space's synced files, in bytes
        (the storage settings; 0 = only what it opens)."""
        self._budget = budget

    def attach_channels(self, channels: _ChannelRing) -> None:
        """v_97 — a GFS space's files come only from the GFS cache: the
        GFSs the space's channel lives on are asked first."""
        self._channels = channels

    def attach_pages(self, page_repo: AbstractPageRepo) -> None:
        """Wire the space pages (v_82): a page's file is kept under its
        page-scoped name (``space_page_media_name``) and fetched only while
        that page (or an open side of it) shows it."""
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
        hashes: Mapping[str, FileHash] | None = None,
    ) -> SpaceMediaLocalization:
        """This household's names for the files item *item_id* of
        *space_id* shows (*media_urls*), as delivered by household *origin*
        — which the CALLER authenticated (the envelope's sender, the signed
        inner's origin, the §25.6 provider), never a descriptor field.
        *hashes* is the item's valid v_85 hash map (by the author's file
        names) as the caller already trusts it, or ``None``.

        * A described file the item has no row for yet → its content name
          (:func:`space_content_media_name` of space, its hash, file name)
          when *hashes* covers it — the one name the file has on every path,
          verified against the hash on fetch — else its reader name
          (:func:`gfs_space_media_name` of space, *origin*, file name); and
          a row to :meth:`keep` once the item is saved.
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
                fh = hashes.get(ref.filename) if hashes else None
                if ref.pieced:
                    # v_97 — a file in pieces is always kept under its
                    # content name: the whole file's checksum. A hash map
                    # saying otherwise is a contradiction — refused.
                    if fh is not None and fh.h != ref.sha256:
                        log.warning(
                            "space media %s of %s: its checksum and hash map"
                            " differ — refused",
                            ref.filename,
                            item_id,
                        )
                        continue
                    local = space_content_media_name(space_id, ref.sha256, ref.filename)
                elif fh is not None:
                    local = space_content_media_name(space_id, fh.h, ref.filename)
                else:
                    local = gfs_space_media_name(
                        space_id, origin, item_id, ref.filename
                    )
                first = held.get(ref.filename)
                if (
                    first is not None
                    and first.local_name == local
                    and first.origin != origin
                ):
                    # The same hashed file another household described
                    # already: one name, its row (and the blob its AAD
                    # names) stays — the bytes must hash to h either way.
                    names[ref.filename] = local
                    continue
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
        """v_82 — the rows for a space page's files (its version's and its
        open sides'), each kept under the page-scoped name
        ``space_page_media_name(space, file)`` — the page itself is never
        rewritten, and its names never address the shared store here. Only
        for the host's version of page *item_id*: the CALLER authenticated
        *origin* as the space's host. A reserved name is never kept; a row
        is bound to the page (write-once, like any row). Never raises."""
        if not item_id or not origin or self._pages is None:
            return SpaceMediaLocalization()
        allowed = {
            name for name in media_names(media_urls) if not is_reserved_media_name(name)
        }
        if not allowed:
            return SpaceMediaLocalization()
        try:
            refs = tuple(
                replace(ref, local_name=space_page_media_name(space_id, ref.filename))
                for ref in parse_media_refs(
                    raw_refs,
                    space_id=space_id,
                    allowed=allowed,
                    item_id=item_id,
                    origin=origin,
                    max_entries=MAX_PAGE_DESCRIPTOR_ENTRIES,
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

    def localize_proposal(
        self,
        space_id: str,
        raw_refs: object,
        added: Mapping[str, str],
        *,
        item_id: str,
        origin: str,
    ) -> SpaceMediaLocalization:
        """v_82, the host — the rows for the files a page proposal from
        household *origin* (reached only through a GFS) ADDS: *added* maps
        each file name it sent to the name it takes here, which must be
        ``space_direct_media_name(space, origin, file)`` — bound to the
        proposer, filled by nobody else. Anything else is dropped; the CALLER
        authenticated *origin* and verified the descriptor's signature.
        Never raises."""
        if not item_id or not origin:
            return SpaceMediaLocalization()
        allowed = {
            name: local
            for name, local in added.items()
            if not is_reserved_media_name(name)
            and local == space_direct_media_name(space_id, origin, name)
        }
        if not allowed:
            return SpaceMediaLocalization()
        try:
            refs = tuple(
                replace(ref, local_name=allowed[ref.filename])
                for ref in parse_media_refs(
                    raw_refs,
                    space_id=space_id,
                    allowed=set(allowed),
                    item_id=item_id,
                    origin=origin,
                    max_entries=MAX_PAGE_FILES,
                )
            )
        except Exception:
            log.warning(
                "space media refs of a proposal for page %s not read",
                item_id,
                exc_info=True,
            )
            return SpaceMediaLocalization()
        return SpaceMediaLocalization(refs=refs)

    async def _shown_by_its_page(self, ref: SpaceMediaRef) -> bool:
        """Whether *ref* (a page-scoped row) belongs to a live page of its
        space that — in its version or an open side — shows that file."""
        if self._pages is None or ref.local_name != space_page_media_name(
            ref.space_id, ref.filename
        ):
            return False
        page = await self._pages.get_space_page(ref.item_id, space_id=ref.space_id)
        if page is None:
            return False
        urls = page_media_urls(page.cover_image_url, page.content)
        for side in await self._pages.list_conflict_sides(
            ref.item_id, space_id=ref.space_id
        ):
            urls.extend(page_media_urls(side.cover_image_url, side.content))
        return ref.filename in media_names(urls)

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
        self,
        loc: SpaceMediaLocalization,
        *,
        prefetch: bool = True,
        hint: SyncHint | None = None,
    ) -> list[SpaceMediaRef]:
        """Keep *loc*'s new rows — call it once the item is stored — and
        sync the files the per-source policy keeps for *hint* (``None``: an
        item that arrived live). The acting host of a GFS space archives
        every file instead, a file in pieces too (v_97, owner 2026-11-28) —
        a submission it holds for review too, which it releases once
        approved. Never raises."""
        if not loc.refs:
            return []
        try:
            await self._refs.save_many(loc.refs)
        except Exception:
            log.warning("space media refs not stored", exc_info=True)
            return []
        archives: dict[str, bool] = {}
        hint = hint or LIVE_HINT
        now = datetime.now(UTC)
        budget = self._budget()
        for ref in loc.refs:
            # A newer epoch may have moved the row: ask the GFS again.
            self._misses.pop(ref.local_name, None)
            if ref.space_id not in archives:
                archives[ref.space_id] = await self._archives(ref.space_id)
            if archives[ref.space_id]:
                self._archive(ref.local_name, ref.size)
            elif (
                prefetch
                and budget > 0
                and syncs(hint, size=ref.size, name=ref.filename, now=now)
            ):
                self._prefetch(
                    ref.local_name,
                    ref.space_id,
                    ref.size,
                    catch_up=hint.source is not SyncSource.LIVE,
                )
        return list(loc.refs)

    async def _archives(self, space_id: str) -> bool:
        """Whether this household keeps every file of *space_id*: it acts
        as the host of a GFS space (:func:`serves_as_host`). ``False`` on a
        read failure — the plain prefetch then."""
        try:
            space = await self._spaces.get(space_id)
            if space is None or not channel_on(space):
                return False
            own = str(getattr(self._federation, "own_instance_id", "") or "")
            return await serves_as_host(self._spaces, space, own_instance_id=own)
        except Exception:
            log.warning("space %s: host role not read", space_id, exc_info=True)
            return False

    def _archive(self, filename: str, size: int, attempts: int = 0) -> None:
        """Queue *filename* for the host's archive (bounded; never raises)."""
        if (
            len(self._archive_queue) >= MAX_HOST_ARCHIVE_PENDING
            or self._archive_bytes + max(size, 0) > MAX_HOST_ARCHIVE_BYTES
        ):
            log.warning(
                "space media host archive full (%d files, %d bytes waiting) — "
                "%s is fetched when someone views it",
                len(self._archive_queue),
                self._archive_bytes,
                filename,
            )
            return
        self._archive_queue.append((filename, max(size, 0), attempts))
        self._archive_bytes += max(size, 0)
        if self._archive_workers < HOST_ARCHIVE_CONCURRENCY:
            self._archive_workers += 1
            task = asyncio.create_task(self._archive_worker())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _archive_worker(self) -> None:
        try:
            while self._archive_queue:
                filename, size, attempts = self._archive_queue.popleft()
                self._archive_bytes -= size
                try:
                    outcome = await self._fetch_task(filename)
                except Exception:
                    log.warning(
                        "space media archive of %s failed", filename, exc_info=True
                    )
                    continue
                if outcome != _MISS:
                    continue
                if attempts < len(HOST_ARCHIVE_RETRY_DELAYS_S):
                    task = asyncio.create_task(
                        self._archive_later(
                            filename,
                            size,
                            attempts + 1,
                            HOST_ARCHIVE_RETRY_DELAYS_S[attempts],
                        )
                    )
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                else:
                    log.warning(
                        "space media %s: the space's host holds no copy — no GFS "
                        "held it after %d tries; fetched when someone views it",
                        filename,
                        attempts + 1,
                    )
        finally:
            self._archive_workers -= 1

    async def _archive_later(
        self, filename: str, size: int, attempts: int, delay: float
    ) -> None:
        await asyncio.sleep(delay)
        self._archive(filename, size, attempts)

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
        if not (
            is_gfs_space_media_name(filename)
            or (self._pages is not None and is_space_page_media_name(filename))
            or (
                (
                    is_space_direct_media_name(filename)
                    or is_space_content_media_name(filename)
                )
                and await self._refs.get(filename) is not None
            )
        ):
            # A file another household pushes (or a local one) that is not
            # here yet: routine for a viewer, never a GFS fetch.
            return False
        if self._misses.get(filename, 0.0) > _monotonic():
            return False  # no GFS held it a moment ago: don't ask them all again
        task = self._fetch_task(filename, want=True)
        try:
            outcome = await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
        except TimeoutError:
            log.warning("space media %s: fetch from the GFS timed out", filename)
            return False
        if outcome in (_MISS, _REFUSED):
            # A refused file (a wrong checksum, a piece that does not open)
            # is not fetched again on every view either (v_97: up to 200
            # MiB each time).
            self._misses[filename] = _monotonic() + MISS_TTL_S
            self._misses.move_to_end(filename)
            while len(self._misses) > MAX_MISSES_REMEMBERED:
                self._misses.popitem(last=False)
        return outcome == _STORED

    def fetching(self, filename: str) -> bool:
        """Whether a fetch of *filename* is running now (v_97: a catch-up
        holds a record back for its next stream while it is)."""
        return any(name == filename for name, _want in self._inflight)

    def _fetch_task(self, filename: str, *, want: bool = False) -> asyncio.Task[str]:
        key = (filename, want)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._fetch_bounded(filename, want=want))
            self._inflight[key] = task
            self._tasks.add(task)

            def _done(t: asyncio.Task, k: tuple[str, bool] = key) -> None:
                self._inflight.pop(k, None)
                self._tasks.discard(t)

            task.add_done_callback(_done)
        return task

    async def _fetch_bounded(self, filename: str, *, want: bool = False) -> str:
        async with self._fetch_sem:
            return await self._fetch(filename, want=want)

    def _prefetch(
        self, filename: str, space_id: str, size: int = 0, *, catch_up: bool = False
    ) -> None:
        size = max(size, 0)
        if (
            self._prefetch_pending >= MAX_PREFETCH_PENDING
            or self._prefetch_bytes + size > MAX_PREFETCH_BYTES
            or self._prefetch_by_space.get(space_id, 0) >= MAX_PREFETCH_PER_SPACE
        ):
            log.info(
                "space media prefetch backlog full — %s waits for a view", filename
            )
            return
        self._prefetch_pending += 1
        self._prefetch_bytes += size
        self._prefetch_by_space[space_id] = self._prefetch_by_space.get(space_id, 0) + 1
        task = asyncio.create_task(
            self._prefetch_one(filename, space_id, size, catch_up=catch_up)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _prefetch_one(
        self, filename: str, space_id: str, size: int, *, catch_up: bool
    ) -> None:
        try:
            async with contextlib.AsyncExitStack() as stack:
                if catch_up:
                    # Behind live content: one catch-up sync at a time.
                    await stack.enter_async_context(self._catch_up_sem)
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
            if outcome == _STORED:
                self._sweep_soon(space_id)
        except Exception:  # pragma: no cover - defensive
            log.warning("space media prefetch of %s failed", filename, exc_info=True)
        finally:
            self._prefetch_pending -= 1
            self._prefetch_bytes -= size
            left = self._prefetch_by_space.get(space_id, 1) - 1
            if left > 0:
                self._prefetch_by_space[space_id] = left
            else:
                self._prefetch_by_space.pop(space_id, None)

    # ── The synced-files budget ──────────────────────────────────────────

    async def note_viewed(self, filename: str) -> None:
        """A person opened *filename*: stamp its ``space_media_refs`` row
        (at most once a day per file). Only a GFS space's file has one."""
        if not (
            is_gfs_space_media_name(filename)
            or is_space_content_media_name(filename)
            or is_space_direct_media_name(filename)
            or is_space_page_media_name(filename)
        ):
            return
        now = _monotonic()
        last = self._viewed.get(filename)
        if last is not None and now - last < VIEW_STAMP_INTERVAL_S:
            return
        self._viewed[filename] = now
        self._viewed.move_to_end(filename)
        while len(self._viewed) > MAX_VIEW_STAMPS_REMEMBERED:
            self._viewed.popitem(last=False)
        try:
            await self._refs.mark_viewed(filename)
        except Exception:
            log.warning("space media %s: view not stamped", filename, exc_info=True)

    def _sweep_soon(self, space_id: str) -> None:
        last = self._swept.get(space_id)
        if last is not None and _monotonic() - last < SWEEP_INTERVAL_S:
            return
        self._swept[space_id] = _monotonic()
        task = asyncio.create_task(self.sweep(space_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def sweep(self, space_id: str) -> int:
        """Bring *space_id*'s synced files within the household's budget:
        never-viewed files first (oldest first), then the least recently
        viewed — never one viewed within :data:`VIEWED_KEEP`, one this
        household delivered itself, one being fetched, nor anything on a
        household acting as the space's host. The rows stay (a view fetches
        the file again). Returns the bytes freed. Never raises."""
        try:
            if await self._archives(space_id):
                return 0
            budget = max(self._budget(), 0)
            held: list[tuple[SpaceMediaRef, pathlib.Path, int]] = []
            total = 0
            for ref in await self._refs.list_for_space(space_id):
                path = media_file_path(self._dir, ref.local_name)
                if path is None:
                    continue
                try:
                    size = (await aiofiles.os.stat(path)).st_size
                except OSError:
                    continue
                total += size
                held.append((ref, path, size))
            if total <= budget:
                return 0
            now = datetime.now(UTC)
            own = str(getattr(self._federation, "own_instance_id", "") or "")

            def order(item: tuple[SpaceMediaRef, pathlib.Path, int]) -> tuple:
                viewed = parse_moment(item[0].viewed_at)
                arrived = parse_moment(item[0].created_at) or now
                return (viewed is not None, viewed or arrived)

            freed = 0
            for ref, path, size in sorted(held, key=order):
                if total - freed <= budget:
                    break
                viewed = parse_moment(ref.viewed_at)
                if (
                    (viewed is not None and now - viewed < VIEWED_KEEP)
                    or (own and ref.origin == own)
                    or self.fetching(ref.local_name)
                ):
                    continue
                await remove_quietly(path)
                freed += size
            if freed:
                log.info(
                    "space media of space %s: %d bytes of synced files removed"
                    " (budget %d bytes)",
                    space_id,
                    freed,
                    budget,
                )
            return freed
        except Exception:
            log.warning("space %s: synced files not swept", space_id, exc_info=True)
            return 0

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
        await wait_tasks_done(lambda: self._tasks)

    async def stop(self) -> None:
        """Let running fetches finish (bounded), then cancel the rest."""
        if not self._tasks:
            return
        _done, pending = await asyncio.wait(list(self._tasks), timeout=5.0)
        for task in pending:
            task.cancel()

    async def _fetch(self, filename: str, *, want: bool = False) -> str:
        """One fetch: ``stored`` (the file is here now), ``miss`` (no GFS
        holds the blob — yet) or ``refused`` (anything else: unknown
        suite, no key, wrong hash, failed decrypt — not worth retrying).
        With *want* (a view), a miss everywhere asks one GFS for it (v_83)."""
        ref = await self._refs.get(filename)
        path = media_file_path(self._dir, filename)
        if ref is None or path is None:
            return _REFUSED
        if not (
            is_gfs_space_media_name(filename)
            or (
                is_space_page_media_name(filename)
                and await self._shown_by_its_page(ref)
            )
            or _bound_to_its_origin(ref)
            or is_space_content_media_name(filename)
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
        sources = await self._sources(ref.space_id)
        if ref.pieced:
            return await self._fetch_pieced(
                ref, scope_id, current[1], path, sources, want
            )
        outcome = _MISS
        for conn in sources:
            blob = await self._gfs.fetch_space_media(
                conn, ref.blob_id, max_bytes=ref.size, timeout_s=REQUEST_TIMEOUT_S
            )
            if blob is None:
                continue
            got = await self._accept(blob, ref, scope_id, current[1], path, conn)
            if got != _MISS:
                return got
            outcome = _REFUSED
        if outcome == _MISS and want and await self._asks_its_gfs(ref):
            return await self._want(ref, scope_id, current[1], path, sources)
        if outcome == _MISS:
            log.info(
                "space media %s: no GFS holds blob %s (yet)", filename, ref.blob_id[:12]
            )
        return outcome

    async def _asks_its_gfs(self, ref: SpaceMediaRef) -> bool:
        """v_83 on-demand is the path of a household that reaches the space
        only through a GFS (P10: decided by PATH, never by role — member or
        follower alike): no seat here, or no paired / mesh path to the host
        (:meth:`FederationService.reaches_host_directly`). A household with
        such a path asks its host or a helper with ``SPACE_MEDIA_WANTED``
        (:class:`~.space_media_wanted.SpaceMediaWanted`) instead — for an
        ``sc-`` content name only, the one name that request carries. It
        keeps the ask for a file it holds under any other name (an ``sg-``
        name taken from the public GFS copy, without a hash map or before
        v_92), and while the space's host is below v_92 and would answer no
        such request (rollout)."""
        if not is_space_content_media_name(ref.local_name):
            return True
        space_id = ref.space_id
        space = await self._spaces.get(space_id)
        if space is not None and channel_on(space):
            # v_97 (owner 2026-11-27) — every household of a GFS space takes
            # its files from the GFS cache, a miss there through on demand;
            # no host / helper push for a GFS space.
            return True
        if not await holds_seat(self._spaces, space_id):
            return True
        if not await self._federation.reaches_host_directly(space_id):
            return True
        host = space.owner_instance_id if space is not None else ""
        if not host:
            return False
        return not await self._federation.space_member_supports(
            host, min_version=FederationCapability.MIN_FOR_SPACE_MEDIA_WANTED
        )

    async def _want(
        self,
        ref: SpaceMediaRef,
        scope_id: str,
        key: bytes,
        path: pathlib.Path,
        sources: list[GfsConnection],
    ) -> str:
        """Ask the first source that proves ``on_demand`` for the blob (a
        holder re-uploads or streams it); checked like a fetch."""
        asked = await self._want_blob(
            ref, ref.blob_id, ref.filename, ref.size, scope_id, key, sources
        )
        if asked is None:
            log.info(
                "space media %s: no GFS holds blob %s (yet)",
                ref.local_name,
                ref.blob_id[:12],
            )
            return _MISS
        blob, conn = asked
        if blob is None:
            return _MISS
        got = await self._accept(blob, ref, scope_id, key, path, conn)
        return _REFUSED if got == _MISS else got

    async def _want_blob(
        self,
        ref: SpaceMediaRef,
        blob_id: str,
        filename: str,
        max_bytes: int,
        scope_id: str,
        key: bytes,
        sources: list[GfsConnection],
    ) -> tuple[bytes | None, GfsConnection] | None:
        """``POST /gfs/media/{blob_id}/wanted`` to the first source proving
        ``on_demand`` — the hint names *filename* (a piece's name for a
        piece). ``(blob or None on a miss, the source)``; ``None`` when no
        source asks."""
        for conn in sources:
            wait = await self._gfs.space_media_on_demand_wait(conn)
            if wait is None:
                continue
            hint = seal_space_media_hint(
                media_key=key,
                scope=ref.scope,
                scope_id=scope_id,
                blob_id=blob_id,
                epoch=ref.epoch,
                locator=SpaceMediaLocator(
                    item_id=ref.item_id, origin=ref.origin, filename=filename
                ),
            )
            blob = await self._gfs.want_space_media(
                conn,
                blob_id,
                {"scope": ref.scope, "scope_id": scope_id, "hint": hint},
                max_bytes=max_bytes,
                timeout_s=wait + REQUEST_TIMEOUT_S,
            )
            if blob is None:
                log.info(
                    "space media %s: GFS %s found no holder for blob %s",
                    ref.local_name,
                    conn.inbox_url,
                    blob_id[:12],
                )
            return blob, conn
        return None

    # ── Files in pieces (v_97) ───────────────────────────────────────────

    async def _fetch_pieced(
        self,
        ref: SpaceMediaRef,
        scope_id: str,
        key: bytes,
        path: pathlib.Path,
        sources: list[GfsConnection],
        want: bool,
    ) -> str:
        """Every piece still missing (the ones already here are kept, so a
        fetch cut short resumes), then the whole file checked against its
        checksum and written once."""
        try:
            count = piece_count(ref.size)
        except ValueError:
            return _REFUSED
        part_dir = self._dir / _PARTIAL_DIR / f"pieces-{ref.local_name}"
        self._active_parts.add(part_dir)
        try:
            have = await present_parts(part_dir)
            for index in range(count):
                if part_name(index) in have:
                    continue
                got = await self._fetch_piece(
                    ref, index, count, scope_id, key, part_dir, sources, want
                )
                if got != _STORED:
                    return got
            return await self._assemble(ref, count, part_dir, path)
        finally:
            self._active_parts.discard(part_dir)

    async def _fetch_piece(
        self,
        ref: SpaceMediaRef,
        index: int,
        count: int,
        scope_id: str,
        key: bytes,
        part_dir: pathlib.Path,
        sources: list[GfsConnection],
        want: bool,
    ) -> str:
        """One piece: from a source that holds it, else (a view) on demand.
        ``stored`` once its bytes open under its piece name and have the
        piece's length."""
        address = piece_address(ref.blob_id, index)
        name = piece_aad_name(ref.filename, index, count)
        expected = (
            PIECE_BYTES if index < count - 1 else ref.size - PIECE_BYTES * (count - 1)
        )
        outcome = _MISS
        for conn in sources:
            blob = await self._gfs.fetch_space_media(
                conn, address, max_bytes=MAX_BLOB_BYTES, timeout_s=PIECE_TIMEOUT_S
            )
            if blob is None:
                continue
            if await self._keep_piece(blob, ref, name, index, expected, key, part_dir):
                return _STORED
            outcome = _REFUSED
        if outcome == _MISS and want:
            asked = await self._want_blob(
                ref, address, name, MAX_BLOB_BYTES, scope_id, key, sources
            )
            if asked is not None and asked[0] is not None:
                if await self._keep_piece(
                    asked[0], ref, name, index, expected, key, part_dir
                ):
                    return _STORED
                return _REFUSED
        if outcome == _MISS:
            log.info(
                "space media %s: no GFS holds piece %d of %d (yet)",
                ref.local_name,
                index,
                count,
            )
        return outcome

    async def _keep_piece(
        self,
        blob: bytes,
        ref: SpaceMediaRef,
        name: str,
        index: int,
        expected: int,
        key: bytes,
        part_dir: pathlib.Path,
    ) -> bool:
        """Open *blob* as piece *name* and keep its bytes as part *index*;
        ``False`` (WARNING) when it does not open there or has another
        length."""
        try:
            data = await asyncio.to_thread(
                _open_blob, blob, key, replace(ref, filename=name)
            )
        except ValueError, InvalidTag:
            log.warning(
                "space media %s: piece %d does not open — refused",
                ref.local_name,
                index,
            )
            return False
        if len(data) != expected:
            log.warning(
                "space media %s: piece %d has the wrong length — refused",
                ref.local_name,
                index,
            )
            return False
        await aiofiles.os.makedirs(part_dir, exist_ok=True)
        stale = await asyncio.to_thread(
            _over_cap_part_dirs,
            self._dir / _PARTIAL_DIR,
            frozenset(self._active_parts | {part_dir}),
            len(data),
        )
        for old in stale:
            await drop_parts(old)
        tmp = part_dir / f".{part_name(index)}.{uuid.uuid4().hex}"
        try:
            async with aiofiles.open(tmp, "wb") as fh:
                await fh.write(data)
            await aiofiles.os.replace(tmp, part_dir / part_name(index))
        finally:
            await remove_quietly(tmp)
        return True

    async def _assemble(
        self, ref: SpaceMediaRef, count: int, part_dir: pathlib.Path, path: pathlib.Path
    ) -> str:
        """Join the pieces into the file, write-once — only when the whole
        hashes to the content name its checksum gave. A mismatch drops
        every piece (which one is wrong can't be told) and writes nothing."""
        tmp = self._dir / _PARTIAL_DIR / f"gfs-{uuid.uuid4().hex}"
        try:
            digest = await asyncio.to_thread(_join_parts_sync, part_dir, count, tmp)
            if not content_name_binds(path.name, ref.space_id, digest):
                log.warning(
                    "space media %s: the joined file does not match its"
                    " checksum — every piece dropped, refused",
                    ref.local_name,
                )
                await drop_parts(part_dir)
                return _REFUSED
            await publish_once(tmp, path)
        finally:
            await remove_quietly(tmp)
        await drop_parts(part_dir)
        log.info(
            "space media %s (%s of %s): joined %d pieces from the GFS",
            ref.local_name,
            ref.filename,
            ref.item_id,
            count,
        )
        return _STORED

    async def _accept(
        self,
        blob: bytes,
        ref: SpaceMediaRef,
        scope_id: str,
        key: bytes,
        path: pathlib.Path,
        conn: GfsConnection,
    ) -> str:
        """Check *blob* (its scope-bound address, then the decrypt) and
        write the file: ``stored``, ``refused`` (it can't open), or ``miss``
        (not at its address — another source may hold the right one)."""
        base_url = conn.inbox_url
        try:
            address = await asyncio.to_thread(
                space_media_address, ref.scope, scope_id, blob, suite=ref.addr_suite
            )
        except ValueError:
            address = ""
        if address != ref.blob_id:
            log.warning(
                "space media %s from %s: not at its %s address — refused",
                ref.local_name,
                base_url,
                ref.scope,
            )
            return _MISS
        try:
            data, h = await asyncio.to_thread(_open_sync, blob, key, ref)
        except ValueError, InvalidTag:
            log.warning(
                "space media %s: cannot decrypt the blob — refused", ref.local_name
            )
            return _REFUSED
        if h is not None and ref.local_name != space_content_media_name(
            ref.space_id, h, ref.filename
        ):
            log.warning(
                "space media %s (%s of %s) from %s: the file does not hash to "
                "its content name — refused",
                ref.local_name,
                ref.filename,
                ref.item_id,
                base_url,
            )
            return _REFUSED
        await self._write(path, data, space_id=ref.space_id, sha256=h)
        log.info(
            "space media %s (%s of %s): fetched blob %s (%d bytes) from GFS %s",
            ref.local_name,
            ref.filename,
            ref.item_id,
            ref.blob_id[:12],
            len(blob),
            base_url,
        )
        return _STORED

    async def _scope_id(self, ref: SpaceMediaRef) -> str | None:
        """The scope id this household itself knows for *ref*'s scope: the
        opaque channel id of its own grant — never one a descriptor or a GFS
        supplies."""
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
        if self._channels is not None:
            # v_97 — the GFSs the space's channel lives on hold its files.
            ring = await self._channels.ring_connections(space_id)
            for held in ring[1] if ring is not None else []:
                if held.inbox_url and held.id not in seen:
                    seen.add(held.id)
                    conns.append(held)
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

    async def _write(
        self, path: pathlib.Path, data: bytes, *, space_id: str, sha256: str | None
    ) -> None:
        await write_once(
            path,
            data,
            self._dir / _PARTIAL_DIR / f"gfs-{uuid.uuid4().hex}",
            space_id=space_id,
            sha256=sha256,
        )


def _over_cap_part_dirs(
    root: pathlib.Path, keep: frozenset[pathlib.Path], adding: int
) -> list[pathlib.Path]:
    """The ``pieces-*`` part directories under *root* to drop, least
    recently written first, so that all of them plus *adding* bytes stay
    within :data:`MAX_PARTIAL_PIECE_BYTES` — never one in *keep* (a fetch
    running now). An entry that vanishes meanwhile (another fetch joined or
    dropped it) is skipped. The worker-thread body (a directory walk)."""
    dirs: list[tuple[float, int, pathlib.Path]] = []
    total = 0
    try:
        entries = list(os.scandir(root))
    except FileNotFoundError:
        return []
    for entry in entries:
        if not entry.name.startswith("pieces-") or not entry.is_dir(
            follow_symlinks=False
        ):
            continue
        size = 0
        newest = 0.0
        try:
            for part in os.scandir(entry.path):
                if part.is_file(follow_symlinks=False):
                    st = part.stat(follow_symlinks=False)
                    size += st.st_size
                    newest = max(newest, st.st_mtime)
        except OSError:
            continue  # gone meanwhile
        total += size
        if pathlib.Path(entry.path) not in keep:
            dirs.append((newest, size, pathlib.Path(entry.path)))
    out: list[pathlib.Path] = []
    for _newest, size, path in sorted(dirs):
        if total + adding <= MAX_PARTIAL_PIECE_BYTES:
            break
        out.append(path)
        total -= size
    return out


def _join_parts_sync(part_dir: pathlib.Path, count: int, tmp: pathlib.Path) -> str:
    """Write parts ``0 … count-1`` of *part_dir* into *tmp*, in order;
    the SHA-256 of the whole. The worker-thread body."""
    os.makedirs(tmp.parent, exist_ok=True)
    whole = hashlib.sha256()
    with open(tmp, "wb") as out:
        for index in range(count):
            with open(part_dir / part_name(index), "rb") as fh:
                data = fh.read()
            whole.update(data)
            out.write(data)
    return whole.hexdigest()


def _bound_to_its_origin(ref: SpaceMediaRef) -> bool:
    """A proposer's file the host fetches (v_82): kept under the name bound
    to the household that sealed it, ``space_direct_media_name(space,
    origin, file)`` — the AAD's household — and nothing else."""
    return bool(ref.origin) and ref.local_name == space_direct_media_name(
        ref.space_id, ref.origin, ref.filename
    )


def _open_sync(blob: bytes, key: bytes, ref: SpaceMediaRef) -> tuple[bytes, str | None]:
    """The file *blob* seals, and — for a content-named (``sc-``) row —
    its bytes' SHA-256 (``None`` otherwise)."""
    data = _open_blob(blob, key, ref)
    if not is_space_content_media_name(ref.local_name):
        return data, None
    return data, sha256_hex(data)


def _open_blob(blob: bytes, key: bytes, ref: SpaceMediaRef) -> bytes:
    return open_space_media(
        blob=blob,
        media_key=key,
        space_id=ref.space_id,
        epoch=ref.epoch,
        item_id=ref.item_id,
        origin=ref.origin,
        filename=ref.filename,
    )
