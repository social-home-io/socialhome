"""Older space files on view — ``SPACE_MEDIA_WANTED`` (v_92).

A member household can hold a space item whose file never reached it: a
push the GFS relay accepted and dropped, a catch-up that skipped the file
for its budget, a picture past the GFS media cache's few days. Before v_92
only the host's 30-minute §25.6 catch-up healed that, and nothing at all
while the host was offline.

**Requester** (:class:`SpaceMediaWanted`). When the media route misses a
content-addressed file (``sc-…``: since v_92 every hashed file is stored
under that one name on every path), :meth:`SpaceMediaWanted.schedule` adds
the name to ONE pending set (deduplicated, at most :data:`MAX_PENDING_NAMES`)
drained by ONE task: after :data:`COALESCE_S` it looks each name up in the
LIVE items of this household that show it
(:meth:`~socialhome.repositories.media_reference_repo.SqliteMediaReferenceRepo
.space_rows_of`), keeps the spaces where this household holds a MEMBER seat
(a follower keeps the GFS's v_83 on-demand path), and sends one request
``{space_id, files: [{item, name}]}`` per :data:`MAX_WANTED_FILES` names of
a space to the space's host — the households acting as host here, owner
first, then the delegated admin that invited us
(:func:`~socialhome.federation.host_equivalence.acting_hosts`, the same
households the scheduler BEGINs and waits on); the first at v_92 that takes
it ends it. A trusted helper (an admin / moderator
household at v_92 from the one shared picker,
:mod:`socialhome.federation.sync.space.trusted_helpers`) gets the same
request ONLY when the host cannot be reached (the send failed) or is silent
(``HostContact.is_silent``: it left the scheduler's last round unanswered)
— never while the host answers, so a helper learns what a member views only
while the host is away. At most :data:`MAX_HELPERS` helpers are tried, the
first that takes the request ends it. Only providers at v_92. A name is
asked again only after :data:`NAME_COOLDOWN_S` — unless it was never queued
(past the pending bound, no live row, a follower seat, a failed lookup):
then the next view asks again. Fixed delays, never random.

**Provider** (:class:`SpaceMediaWantedProvider`). The requester must hold a
writer (member) seat in the space — the predicate the §25.6 provider uses
for member-only data (:meth:`SpaceWriterHouseholds.may_receive`), so a
follower-only household gets nothing (it asks its GFS); each entry must
name a LIVE item of THAT space showing a file whose content name is the
requested one (a stored ``sc-`` name directly; an unhashed local file by
``space_content_media_name(space, sha256(file), basename)``); the file gate
(:class:`~socialhome.federation.sync.space.file_gate.CatchUpFileGates`,
with fills: the bytes are proven) decides what this household may ship
(host: all; a trusted household: other people's files too; anyone: its own
people's); and ONE budget per (requester, space) shared with the resume's
files (:class:`~socialhome.federation.sync.space.file_gate.SpaceFileBudget`,
50 files / 32 MiB per 30 minutes) bounds the upload (a request arriving
with the window spent is dropped before any query or hash). The files go
out as ordinary ``SPACE_MEDIA_BLOB`` pushes from the media outbox with
``push=True`` (never dropped for "reads from its GFS" — the requester
already missed it). The receiver checks the bytes against the hash its row
carries (v_86 fill rule).

**Cost.** One old file on view: one request (< 1 KiB) to the host and the
file's bytes once (+ at most :data:`MAX_HELPERS` more requests while the
host is offline). A batch of 50 files: one request, ≤ 32 MiB once. Per
(requester, space) ≤ 32 MiB / 30 min per provider, resume files included.

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

from ..domain.federation import FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..domain.file_hashes import (
    is_space_content_media_name,
    space_content_media_name,
)
from ..federation.host_equivalence import acting_hosts
from ..federation.sync.space.file_gate import (
    CatchUpFileGates,
    SpaceFileBudget,
    local_file_size,
)
from ..federation.sync.space.trusted_helpers import helper_offset, trusted_helpers
from .inbound_media_store import is_safe_media_name
from .space_writer_households import holds_member_seat

if TYPE_CHECKING:
    from ..domain.federation import DeliveryResult, FederationEvent
    from ..repositories.space_remote_member_repo import AbstractSpaceRemoteMemberRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from .media_file_hasher import MediaFileHasher

log = logging.getLogger(__name__)

#: Most ``{item, name}`` entries in one request (sender and receiver).
MAX_WANTED_FILES: int = 50

#: How long names are collected before they are looked up and asked for.
COALESCE_S: float = 2.0

#: Helpers tried while the host is silent or unreachable.
MAX_HELPERS: int = 2

#: A name is asked for again only after this long (repeated views).
NAME_COOLDOWN_S: float = 10 * 60.0

#: Names waiting to be asked for (deduplicated); past it a view's name is
#: dropped WITHOUT a cooldown — its next view asks again. The cooldown map
#: is bounded the same way.
MAX_PENDING_NAMES: int = 500

#: Longest item id a provider looks up.
MAX_ITEM_ID_LEN: int = 200


class _SpaceRows(Protocol):
    async def space_rows_of(self, basename: str) -> list[tuple[str, str, str]]: ...


class _ItemFiles(Protocol):
    async def live_item_files(
        self, space_id: str, item_id: str
    ) -> list[tuple[str, str | None]]: ...


class _HostContact(Protocol):
    def is_silent(self, space_id: str) -> bool: ...


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


class SpaceMediaWanted:
    """Ask the host (or, while it is away, a helper) for space files this
    member household misses."""

    __slots__ = (
        "_asked",
        "_clock",
        "_coalesce_s",
        "_contact",
        "_federation",
        "_names",
        "_own",
        "_refs",
        "_seats",
        "_spaces",
        "_stop",
        "_task",
    )

    def __init__(
        self,
        *,
        media_refs: _SpaceRows,
        space_repo: AbstractSpaceRepo,
        remote_members: AbstractSpaceRemoteMemberRepo,
        federation: _Federation,
        host_contact: _HostContact,
        own_instance_id: str,
        clock: Callable[[], float] = time.monotonic,
        coalesce_s: float = COALESCE_S,
    ) -> None:
        self._refs = media_refs
        self._spaces = space_repo
        self._seats = remote_members
        self._federation = federation
        self._contact = host_contact
        self._own = own_instance_id
        self._clock = clock
        self._coalesce_s = coalesce_s
        #: name → when it was last asked for (:data:`NAME_COOLDOWN_S`).
        self._asked: OrderedDict[str, float] = OrderedDict()
        #: names waiting to be asked for (an ordered set).
        self._names: dict[str, None] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def schedule(self, basename: str) -> None:
        """Fire-and-forget: ask for *basename*, a file a view just missed.
        Never raises, never delays the view."""
        if self._stop.is_set() or basename in self._names:
            return
        if not is_space_content_media_name(basename) or not is_safe_media_name(
            basename
        ):
            return
        last = self._asked.get(basename)
        if last is not None and self._clock() - last < NAME_COOLDOWN_S:
            return
        if len(self._names) >= MAX_PENDING_NAMES:
            log.info(
                "space media wanted: %d names wait — %s not asked for now",
                len(self._names),
                basename,
            )
            return
        self._names[basename] = None
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(
                self._drain(), name="space-media-wanted"
            )

    async def drain(self) -> None:
        """Wait until every name scheduled so far is asked for (tests, the
        demo harness)."""
        while self._task is not None and not self._task.done():
            await asyncio.gather(self._task, return_exceptions=True)

    async def stop(self) -> None:
        """Stop: names still waiting are dropped; nothing new starts."""
        self._stop.set()
        self._names.clear()
        await self.drain()

    async def _drain(self) -> None:
        while self._names and not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._coalesce_s)
                return  # stopped while collecting
            except TimeoutError:
                pass
            names = list(self._names)
            self._names.clear()
            now = self._clock()
            for name in names:
                self._asked[name] = now
                self._asked.move_to_end(name)
            while len(self._asked) > MAX_PENDING_NAMES:
                self._asked.popitem(last=False)
            per_space: dict[str, dict[str, str]] = {}
            for name in names:
                if not await self._lookup(name, per_space):
                    # Nothing asked for it: the next view may try again.
                    self._asked.pop(name, None)
            for space_id, files in per_space.items():
                entries = list(files.items())
                for i in range(0, len(entries), MAX_WANTED_FILES):
                    try:
                        await self._deliver(space_id, entries[i : i + MAX_WANTED_FILES])
                    except Exception:
                        log.warning(
                            "space media wanted: request for space %s failed",
                            space_id,
                            exc_info=True,
                        )

    async def _lookup(self, name: str, per_space: dict[str, dict[str, str]]) -> bool:
        """Add *name* to ``per_space`` for every space this household holds a
        member seat in whose live item shows it; whether it was added."""
        added = False
        try:
            for space_id, item_id, _created in await self._refs.space_rows_of(name):
                if name in per_space.get(space_id, {}):
                    continue
                if not await holds_member_seat(self._spaces, space_id):
                    continue  # a follower reads through its GFS (v_83)
                per_space.setdefault(space_id, {})[name] = item_id
                added = True
        except Exception:
            log.warning("space media wanted: lookup of %s failed", name, exc_info=True)
        return added

    async def _deliver(self, space_id: str, files: list[tuple[str, str]]) -> None:
        """Ask the host for *files* (``(name, item)``) — the households
        acting as host here, owner first (:func:`acting_hosts`, the same
        notion the scheduler's silent-host wait uses). A helper instead only
        when the host is silent (its last sync round went unanswered — a send
        to its GFS mailbox would "succeed" with nobody to answer) or none of
        the hosts at v_92 could be reached; one batch never goes to both
        (a silent host gets it only when no helper took it)."""
        space = await self._spaces.get(space_id)
        if (
            space is None
            or not space.owner_instance_id
            or space.owner_instance_id == self._own
        ):
            return
        hosts = [
            h
            for h in await acting_hosts(
                self._spaces, self._seats, space, own_instance_id=self._own
            )
            if h and h != self._own
        ]
        payload = {
            "space_id": space_id,
            "files": [{"item": item, "name": name} for name, item in files],
        }
        silent = self._contact.is_silent(space_id)
        if not silent:
            current = False
            for host in await self._current(hosts):
                current = True
                if await self._send(host, space_id, payload):
                    return
            if not current:
                return  # an older host that answers heals through its catch-up
        helpers = await trusted_helpers(
            self._seats,
            self._federation,
            space_id,
            exclude={*hosts, self._own},
            offset=helper_offset(self._own, space_id),
        )
        for helper in helpers[:MAX_HELPERS]:
            if await self._send(helper, space_id, payload):
                return
        if silent:
            for host in await self._current(hosts):
                if await self._send(host, space_id, payload):
                    return
        log.info(
            "space media wanted: the host of space %s is away and no helper at "
            "v_%d took %d file(s) — they wait for the catch-up",
            space_id,
            FederationCapability.MIN_FOR_SPACE_MEDIA_WANTED,
            len(files),
        )

    async def _current(self, hosts: list[str]) -> list[str]:
        """The *hosts* at v_92 (who can answer a request), in order."""
        return [
            h
            for h in hosts
            if await self._federation.space_member_supports(
                h, min_version=FederationCapability.MIN_FOR_SPACE_MEDIA_WANTED
            )
        ]

    async def _send(self, to: str, space_id: str, payload: dict) -> bool:
        result = await self._federation.send_with_mesh_fallback(
            to_instance_id=to,
            event_type=FederationEventType.SPACE_MEDIA_WANTED,
            payload=payload,
            space_id=space_id,
        )
        log.info(
            "space media wanted: %d file(s) of space %s to %s: %s",
            len(payload["files"]),
            space_id,
            to,
            "sent" if result.ok else result.error,
        )
        return result.ok


class SpaceMediaWantedProvider:
    """Answer a member's ``SPACE_MEDIA_WANTED`` with the files we may ship."""

    __slots__ = (
        "_budget",
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
        file_gates: CatchUpFileGates,
        hasher: MediaFileHasher,
        media_sync: _MediaOutbox,
        media_dir: pathlib.Path,
        budget: SpaceFileBudget,
    ) -> None:
        #: ``(space, household) → holds a writer seat`` — the §25.6 gate for
        #: member-only data (:meth:`SpaceWriterHouseholds.may_receive`).
        self._member_gate = member_gate
        self._refs = media_refs
        self._gates = file_gates
        self._hasher = hasher
        self._outbox = media_sync
        self._dir = pathlib.Path(media_dir)
        #: The (requester, space) budget shared with the resume's files.
        self._budget = budget

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
        if self._budget.exhausted(requester, space_id):
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
        gate = None
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
                    # ``_match`` proved the bytes' content name is *name*: a
                    # trusted household may fill it, whatever it is stored as.
                    gate = await self._gates.for_space(space_id, fills=True)
                size = await local_file_size(self._dir, url)
                if not gate.ship(owner, [url]) or size is None:
                    skipped += 1
                    continue
                if not self._budget.take(requester, space_id, size):
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
    "MAX_HELPERS",
    "MAX_WANTED_FILES",
    "NAME_COOLDOWN_S",
    "SpaceMediaWanted",
    "SpaceMediaWantedProvider",
]
