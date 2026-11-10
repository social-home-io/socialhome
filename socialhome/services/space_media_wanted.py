"""Older space files on view — ``SPACE_MEDIA_WANTED`` (v_92).

A member household can hold a space item whose file never reached it: a
push the GFS relay accepted and dropped, a catch-up that skipped the file
for its budget, a picture past the GFS media cache's few days. Before v_92
only the host's 30-minute §25.6 catch-up healed that, and nothing at all
while the host was offline.

**Requester** (:class:`SpaceMediaWanted`). When the media route misses a
content-addressed file (``sc-…``: since v_92 every hashed file is stored
under that one name on every path), :meth:`SpaceMediaWanted.schedule` looks
up the LIVE items of this household that show it
(:meth:`~socialhome.repositories.media_reference_repo.SqliteMediaReferenceRepo
.space_rows_of`) and, for each space where this household holds a MEMBER
seat (a follower keeps the GFS's v_83 on-demand path), collects the
``(item, name)`` pair. Names of one space are coalesced for
:data:`COALESCE_S` into ONE request ``{space_id, files: [{item, name}]}``
(a batch that reaches :data:`MAX_WANTED_FILES` goes at once and the next
name opens a new batch), sent to the space's host first; after
:data:`PROVIDER_WAIT_S` the names still not stored here go to the next
provider — a trusted (admin / moderator) household at v_92 from the one
shared picker (:mod:`socialhome.federation.sync.space.trusted_helpers`:
sorted, from this household's stable offset) — at most
:data:`MAX_PROVIDERS` providers per batch, only providers at v_92 (a failed
send moves on at once). Views queue their names (deduplicated, at most
:data:`MAX_PENDING_NAMES`) for :data:`MAX_LOOKUP_WORKERS` lookup workers, so
a burst of misses is asked for in full. A name is asked again only after
:data:`NAME_COOLDOWN_S` — unless it was never queued (past the queue bound,
no live row, a follower seat, the batch cap, a failed lookup): then the
next view asks again. Fixed delays, never random.

**Provider** (:class:`SpaceMediaWantedProvider`). The requester must hold a
writer (member) seat in the space — the predicate the §25.6 provider uses
for member-only data (:meth:`SpaceWriterHouseholds.may_receive`), so a
follower-only household gets nothing (it asks its GFS); each entry must
name a LIVE item of THAT space showing a file whose content name is the
requested one (a stored ``sc-`` name directly; an unhashed local file by
``space_content_media_name(space, sha256(file), basename)``); the v_86 file
gate (:class:`~socialhome.federation.sync.space.file_gate.CatchUpFileGates`)
decides what this household may ship (host: all; a trusted household:
other people's hashed files; anyone: its own people's); and a budget per
(requester, space) of a resume's 50 files / 32 MiB per 30 minutes bounds the
upload (a request arriving with the window spent is dropped before any
query or hash). The files go out as ordinary ``SPACE_MEDIA_BLOB`` pushes from the
media outbox with ``push=True`` (never dropped for "reads from its GFS" —
the requester already missed it). The receiver checks the bytes against the
hash its row carries (v_86 fill rule).

**Cost.** One old file on view: one request (< 1 KiB) to one provider and
the file's bytes once (+ at most ``MAX_PROVIDERS - 1`` more requests when
the host is offline or does not have it). A batch of 50 files: one request,
≤ 32 MiB once. Per (requester, space) ≤ 32 MiB / 30 min per provider.

It replaces the never-released v_87 relay media refill (a resume scoped to
one resource, host-only).
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

import aiofiles.os

from ..domain.federation import FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..domain.file_hashes import (
    is_space_content_media_name,
    space_content_media_name,
)
from ..federation.sync.space.file_gate import local_file_size
from ..federation.sync.space.resume import (
    RESUME_COOLDOWN_S,
    RESUME_COOLDOWN_TRACKED,
    RESUME_MEDIA_MAX_BYTES,
    RESUME_MEDIA_MAX_FILES,
)
from ..federation.sync.space.trusted_helpers import helper_offset, trusted_helpers
from .inbound_media_store import is_safe_media_name, media_file_path
from .space_writer_households import holds_member_seat

if TYPE_CHECKING:
    from ..domain.federation import DeliveryResult, FederationEvent
    from ..repositories.space_remote_member_repo import AbstractSpaceRemoteMemberRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from .media_file_hasher import MediaFileHasher

log = logging.getLogger(__name__)

#: Most ``{item, name}`` entries in one request (sender and receiver).
MAX_WANTED_FILES: int = 50

#: How long names of one space are collected before the request goes.
COALESCE_S: float = 2.0

#: How long one provider gets to deliver before the names still missing go
#: to the next one.
PROVIDER_WAIT_S: float = 60.0

#: Providers asked per batch (the host and two helpers).
MAX_PROVIDERS: int = 3

#: A name is asked for again only after this long (repeated views).
NAME_COOLDOWN_S: float = 10 * 60.0

#: Names whose last ask is remembered (oldest dropped).
MAX_TRACKED_NAMES: int = 4096

#: Names waiting for their lookup (deduplicated); past it a view's name is
#: dropped WITHOUT a cooldown — its next view asks again.
MAX_PENDING_NAMES: int = 500

#: Workers draining those names (lookups in flight at once).
MAX_LOOKUP_WORKERS: int = 8

#: Batches waiting or being delivered at once; past it a name is dropped
#: until its cooldown ends.
MAX_BATCH_TASKS: int = 64

#: Longest item id a provider looks up.
MAX_ITEM_ID_LEN: int = 200


class _SpaceRows(Protocol):
    async def space_rows_of(self, basename: str) -> list[tuple[str, str, str]]: ...


class _ItemFiles(Protocol):
    async def live_item_files(
        self, space_id: str, item_id: str
    ) -> list[tuple[str, str | None]]: ...


class _Federation(Protocol):
    async def space_member_supports(
        self, instance_id: str, *, min_version: int
    ) -> bool: ...

    async def send_with_mesh_fallback(
        self,
        *,
        to_instance_id: str,
        event_type: FederationEventType,
        payload: dict,
        space_id: str | None = None,
    ) -> DeliveryResult: ...


class _FileGate(Protocol):
    async def ship(
        self, owner: str | None, urls: list[str], *, hashed: bool = False
    ) -> list[str]: ...


class _FileGates(Protocol):
    async def for_requester(self, space_id: str, requester: str) -> _FileGate: ...


class _MediaOutbox(Protocol):
    async def enqueue_for_blob(
        self,
        *,
        space_id: str,
        correlation_id: str,
        target_instance_ids: list[str],
        media_urls: list[str],
        push: bool = False,
        wire_names: dict[str, str] | None = None,
    ) -> None: ...


class _Batch:
    """The names of one space collected for one request."""

    __slots__ = ("files", "full")

    def __init__(self) -> None:
        #: name → the item that shows it here.
        self.files: dict[str, str] = {}
        self.full = asyncio.Event()


class SpaceMediaWanted:
    """Ask a provider for space files this member household misses."""

    __slots__ = (
        "_asked",
        "_batches",
        "_clock",
        "_coalesce_s",
        "_dir",
        "_federation",
        "_own",
        "_pending",
        "_provider_wait_s",
        "_queued",
        "_refs",
        "_seats",
        "_spaces",
        "_stop",
        "_workers",
    )

    def __init__(
        self,
        *,
        media_refs: _SpaceRows,
        space_repo: AbstractSpaceRepo,
        remote_members: AbstractSpaceRemoteMemberRepo,
        federation: _Federation,
        own_instance_id: str,
        media_dir: pathlib.Path,
        clock: Callable[[], float] = time.monotonic,
        coalesce_s: float = COALESCE_S,
        provider_wait_s: float = PROVIDER_WAIT_S,
    ) -> None:
        self._refs = media_refs
        self._spaces = space_repo
        self._seats = remote_members
        self._federation = federation
        self._own = own_instance_id
        self._dir = pathlib.Path(media_dir)
        self._clock = clock
        self._coalesce_s = coalesce_s
        self._provider_wait_s = provider_wait_s
        #: name → when it was last asked for (:data:`NAME_COOLDOWN_S`).
        self._asked: OrderedDict[str, float] = OrderedDict()
        #: space → the batch still collecting names.
        self._pending: dict[str, _Batch] = {}
        #: names waiting for their lookup, oldest first (≤ MAX_PENDING_NAMES).
        self._queued: OrderedDict[str, None] = OrderedDict()
        #: the lookup workers draining it (≤ MAX_LOOKUP_WORKERS).
        self._workers: set[asyncio.Task[None]] = set()
        self._batches: set[asyncio.Task[None]] = set()
        self._stop = asyncio.Event()

    # ── Requests ─────────────────────────────────────────────────────────

    def schedule(self, basename: str) -> None:
        """Fire-and-forget: ask for *basename*, a file a view just missed.
        Never raises, never delays the view."""
        if self._stop.is_set():
            return
        if not is_space_content_media_name(basename) or not is_safe_media_name(
            basename
        ):
            return
        now = self._clock()
        last = self._asked.get(basename)
        if last is not None and now - last < NAME_COOLDOWN_S:
            return
        if basename in self._queued:
            return
        if len(self._queued) >= MAX_PENDING_NAMES:
            # Dropped without a cooldown: the next view asks again.
            log.info(
                "space media wanted: %d names wait for a lookup — %s not asked for now",
                len(self._queued),
                basename,
            )
            return
        self._asked[basename] = now
        self._asked.move_to_end(basename)
        while len(self._asked) > MAX_TRACKED_NAMES:
            self._asked.popitem(last=False)
        self._queued[basename] = None
        if len(self._workers) < MAX_LOOKUP_WORKERS:
            task = asyncio.get_running_loop().create_task(
                self._work(), name="space-media-wanted-lookup"
            )
            self._workers.add(task)

    async def drain(self) -> None:
        """Wait until every lookup and batch scheduled so far is done
        (tests, the demo harness)."""
        while self._workers or self._batches:
            await asyncio.gather(
                *list(self._workers), *list(self._batches), return_exceptions=True
            )

    async def stop(self) -> None:
        """Stop: a waiting batch ends without sending; nothing new starts."""
        self._stop.set()
        self._queued.clear()
        await self.drain()

    async def _work(self) -> None:
        """Look up the queued names, oldest first, until none is left. The
        worker leaves the worker set in the same step it finds the queue
        empty, so a name queued after that always starts a new worker."""
        try:
            while self._queued and not self._stop.is_set():
                basename, _ = self._queued.popitem(last=False)
                await self._lookup(basename)
        finally:
            task = asyncio.current_task()
            if task is not None:
                self._workers.discard(task)

    async def _lookup(self, basename: str) -> None:
        queued = False
        try:
            rows = await self._refs.space_rows_of(basename)
            seen: set[str] = set()
            for space_id, item_id, _created in rows:
                if space_id in seen:
                    continue
                seen.add(space_id)
                if not await holds_member_seat(self._spaces, space_id):
                    continue  # a follower reads through its GFS (v_83)
                queued = self._add(space_id, item_id, basename) or queued
        except Exception:
            log.warning(
                "space media wanted: lookup of %s failed", basename, exc_info=True
            )
        if not queued:
            # Nothing asked for it: the next view may try again at once.
            self._asked.pop(basename, None)

    def _add(self, space_id: str, item_id: str, name: str) -> bool:
        """Queue *name* in the space's collecting batch (a new one when
        none is collecting); whether it was queued. A batch that fills is
        detached at once, so the next name opens a new batch."""
        batch = self._pending.get(space_id)
        if batch is None:
            if self._stop.is_set() or len(self._batches) >= MAX_BATCH_TASKS:
                log.info(
                    "space media wanted: %d batches in flight — %s not asked for",
                    len(self._batches),
                    name,
                )
                return False
            batch = _Batch()
            self._pending[space_id] = batch
            task = asyncio.get_running_loop().create_task(
                self._run(space_id, batch), name="space-media-wanted-batch"
            )
            self._batches.add(task)
            task.add_done_callback(self._batches.discard)
        batch.files.setdefault(name, item_id)
        if len(batch.files) >= MAX_WANTED_FILES:
            del self._pending[space_id]
            batch.full.set()
        return True

    async def _run(self, space_id: str, batch: _Batch) -> None:
        try:
            await self._pause(self._coalesce_s, batch.full)
            if self._pending.get(space_id) is batch:
                del self._pending[space_id]
            if self._stop.is_set() or not batch.files:
                return
            await self._deliver(space_id, list(batch.files.items()))
        except Exception:
            log.warning(
                "space media wanted: request for space %s failed",
                space_id,
                exc_info=True,
            )

    async def _deliver(self, space_id: str, files: list[tuple[str, str]]) -> None:
        """Ask the providers one after the other for *files* (``(name,
        item)``) until every file is stored here or the providers run out."""
        providers = await self._providers(space_id)
        if not providers:
            log.info(
                "space media wanted: no provider at v_%d for space %s — %d "
                "file(s) wait for the catch-up",
                FederationCapability.MIN_FOR_SPACE_MEDIA_WANTED,
                space_id,
                len(files),
            )
            return
        remaining = files
        for index, provider in enumerate(providers):
            payload = {
                "space_id": space_id,
                "files": [{"item": item, "name": name} for name, item in remaining],
            }
            result = await self._federation.send_with_mesh_fallback(
                to_instance_id=provider,
                event_type=FederationEventType.SPACE_MEDIA_WANTED,
                payload=payload,
                space_id=space_id,
            )
            if not result.ok:
                log.info(
                    "space media wanted: %s unreachable for space %s (%s) — "
                    "asking the next provider",
                    provider,
                    space_id,
                    result.error,
                )
                continue
            log.info(
                "space media wanted: asked %s for %d file(s) of space %s",
                provider,
                len(remaining),
                space_id,
            )
            if index == len(providers) - 1:
                return
            await self._pause(self._provider_wait_s)
            if self._stop.is_set():
                return
            remaining = [(n, i) for n, i in remaining if not await self._have(n)]
            if not remaining:
                return

    async def _providers(self, space_id: str) -> list[str]:
        """The space's host, then its trusted helpers (the one shared
        picker, :func:`trusted_helpers`, from this household's offset),
        never this household — only those at v_92, at most
        :data:`MAX_PROVIDERS`."""
        space = await self._spaces.get(space_id)
        host = space.owner_instance_id if space is not None else ""
        out: list[str] = []
        if (
            host
            and host != self._own
            and await self._federation.space_member_supports(
                host, min_version=FederationCapability.MIN_FOR_SPACE_MEDIA_WANTED
            )
        ):
            out.append(host)
        helpers = await trusted_helpers(
            self._seats,
            self._federation,
            space_id,
            exclude={host, self._own},
            offset=helper_offset(self._own, space_id),
        )
        return (out + helpers)[:MAX_PROVIDERS]

    async def _have(self, name: str) -> bool:
        path = media_file_path(self._dir, name)
        return path is not None and await aiofiles.os.path.isfile(path)

    async def _pause(self, seconds: float, event: asyncio.Event | None = None) -> None:
        """Wait *seconds*, less when *event* or the stop is set."""
        if seconds <= 0 or self._stop.is_set():
            return
        waiters = [asyncio.ensure_future(self._stop.wait())]
        if event is not None:
            waiters.append(asyncio.ensure_future(event.wait()))
        try:
            await asyncio.wait(
                waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for waiter in waiters:
                waiter.cancel()


class SpaceMediaWantedProvider:
    """Answer a member's ``SPACE_MEDIA_WANTED`` with the files we may ship."""

    __slots__ = (
        "_budget",
        "_clock",
        "_dir",
        "_gates",
        "_hasher",
        "_member_gate",
        "_outbox",
        "_refs",
    )

    def __init__(
        self,
        *,
        member_gate: Callable[[str, str], Awaitable[bool]],
        media_refs: _ItemFiles,
        file_gates: _FileGates,
        hasher: MediaFileHasher,
        media_sync: _MediaOutbox,
        media_dir: pathlib.Path,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        #: ``(space, household) → holds a writer seat`` — the §25.6 gate for
        #: member-only data (:meth:`SpaceWriterHouseholds.may_receive`).
        self._member_gate = member_gate
        self._refs = media_refs
        self._gates = file_gates
        self._hasher = hasher
        self._outbox = media_sync
        self._dir = pathlib.Path(media_dir)
        self._clock = clock
        #: (requester, space) → [window start, files, bytes] (a resume's
        #: budget, :data:`RESUME_COOLDOWN_S` windows).
        self._budget: OrderedDict[tuple[str, str], list[float]] = OrderedDict()

    async def handle(self, event: FederationEvent) -> int:
        """Queue the asked-for files we hold and may ship; how many."""
        payload = event.payload or {}
        space_id = str(event.space_id or payload.get("space_id") or "")
        files = payload.get("files")
        requester = event.from_instance
        if (
            not space_id
            or not isinstance(files, list)
            or not 1 <= len(files) <= MAX_WANTED_FILES
        ):
            log.info("SPACE_MEDIA_WANTED from %s: malformed — dropped", requester)
            return 0
        if self._exhausted(requester, space_id):
            # Before any query or hash: the window is spent.
            log.info(
                "SPACE_MEDIA_WANTED from %s for space %s: over the budget — dropped",
                requester,
                space_id,
            )
            return 0
        if not await self._member_gate(space_id, requester):
            # A follower-only household reads through its GFS (v_83).
            log.info(
                "SPACE_MEDIA_WANTED from %s: holds no member seat in space %s "
                "— dropped",
                requester,
                space_id,
            )
            return 0
        gate: _FileGate | None = None
        seen: set[str] = set()
        queued = skipped = over = 0
        for entry in files:
            parsed = _entry(entry)
            if parsed is None or parsed[1] in seen:
                skipped += 1
                continue
            item_id, name = parsed
            try:
                found = await self._match(space_id, item_id, name)
                if found is None:
                    skipped += 1
                    continue
                url, owner = found
                if gate is None:
                    gate = await self._gates.for_requester(space_id, requester)
                # ``_match`` proved the bytes' content name is *name*: a
                # hashed file whatever name it is stored under here.
                if not await gate.ship(owner, [url], hashed=True):
                    skipped += 1
                    continue
                size = await local_file_size(self._dir, url)
                if size is None:
                    skipped += 1
                    continue
                if not self._take(requester, space_id, size):
                    over += 1
                    continue
                await self._outbox.enqueue_for_blob(
                    space_id=space_id,
                    correlation_id=item_id,
                    target_instance_ids=[requester],
                    media_urls=[url],
                    push=True,
                    # Sent as the asked-for content name: a helper may store
                    # it under its own reader name (``sg-``), which the
                    # requester refuses by name.
                    wire_names={url: name},
                )
                seen.add(name)  # one file is queued once per request
                queued += 1
            except Exception:
                log.warning(
                    "SPACE_MEDIA_WANTED from %s: an entry of space %s failed",
                    requester,
                    space_id,
                    exc_info=True,
                )
                skipped += 1
        log.info(
            "SPACE_MEDIA_WANTED from %s for space %s: %d file(s) queued, %d "
            "skipped, %d over the budget",
            requester,
            space_id,
            queued,
            skipped,
            over,
        )
        return queued

    async def _match(
        self, space_id: str, item_id: str, name: str
    ) -> tuple[str, str | None] | None:
        """The local file of the live item *item_id* of *space_id* whose
        content name is *name*, with the item's owner — or ``None``."""
        for basename, owner in await self._refs.live_item_files(space_id, item_id):
            if basename == name:
                return basename, owner
            if is_space_content_media_name(basename):
                continue  # another content name: not this file
            found = await self._hasher.hash_name(basename)
            if found is not None and (
                space_content_media_name(space_id, found.h, basename) == name
            ):
                return basename, owner
        return None

    def _exhausted(self, requester: str, space_id: str) -> bool:
        """Whether the (requester, space) window can take no file at all."""
        window = self._budget.get((requester, space_id))
        if window is None or self._clock() - window[0] >= RESUME_COOLDOWN_S:
            return False
        return (
            window[1] >= RESUME_MEDIA_MAX_FILES or window[2] >= RESUME_MEDIA_MAX_BYTES
        )

    def _take(self, requester: str, space_id: str, size: int) -> bool:
        """Charge one file of *size* bytes to the (requester, space) budget,
        or ``False`` when it would pass it."""
        key = (requester, space_id)
        now = self._clock()
        window = self._budget.get(key)
        if window is None or now - window[0] >= RESUME_COOLDOWN_S:
            window = [now, 0.0, 0.0]
            self._budget[key] = window
        self._budget.move_to_end(key)
        while len(self._budget) > RESUME_COOLDOWN_TRACKED:
            self._budget.popitem(last=False)
        if window[1] + 1 > RESUME_MEDIA_MAX_FILES or (
            window[2] + size > RESUME_MEDIA_MAX_BYTES
        ):
            return False
        window[1] += 1
        window[2] += size
        return True


def _entry(raw: Any) -> tuple[str, str] | None:
    """``(item, name)`` of a well-formed entry, else ``None``."""
    if not isinstance(raw, dict):
        return None
    item_id, name = raw.get("item"), raw.get("name")
    if not isinstance(item_id, str) or not 0 < len(item_id) <= MAX_ITEM_ID_LEN:
        return None
    if not isinstance(name, str) or not is_space_content_media_name(name):
        return None
    if not is_safe_media_name(name):
        return None
    return item_id, name


__all__ = [
    "COALESCE_S",
    "MAX_PROVIDERS",
    "MAX_WANTED_FILES",
    "NAME_COOLDOWN_S",
    "PROVIDER_WAIT_S",
    "SpaceMediaWanted",
    "SpaceMediaWantedProvider",
]
