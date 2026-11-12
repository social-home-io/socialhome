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

**Without a view** (:meth:`SpaceMediaWanted.applied`). A push is done for
its sender once the first hop took it; a relay that then drops it (the
target stayed away past the mesh's pending window) tells nobody. So the
receiving side closes the gap: every verified inbound event of a space —
a live row, a sync or resume chunk, a helper's catch-up — marks the space
(``FederationService._dispatch_event``), and ONE check per marked space,
:data:`APPLY_GRACE_S` after the first mark (a row's file normally follows
it; by then its push arrived or is lost), reads the newest live items of
the last :data:`SWEEP_MAX_AGE_S`
(:meth:`~socialhome.repositories.media_reference_repo.SqliteMediaReferenceRepo
.recent_item_files`: the newest :data:`MAX_WANTED_FILES` rows of each kind
— a missing file of an older row heals on view) and asks — the same
request, the same routing as a view — for the askable names that are not
in the media dir: at most :data:`MAX_WANTED_FILES` per check, never a file
this household reads from a GFS media cache (it holds a
``space_media_refs`` row: prefetched, or fetched on view), never for a
follower seat. A row created less than the grace ago is left out (its push
may still be on its way) and the space is checked again a grace later. A
name is settled — not asked again without a view — once a provider
ACCEPTED its request; a request nobody took is made again by the next
check, :data:`MAX_CHECK_ATTEMPTS` times in all. Cost: nothing on the wire
while every file is here; per lost file one request entry and its bytes
once.

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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

import aiofiles.os

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
from .inbound_media_store import (
    is_safe_media_name,
    is_space_direct_media_name,
    media_file_path,
    space_direct_media_name,
)
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

#: How long after a space's first inbound event its rows are checked for
#: files that are not here: past the mesh's pending window (270 s), so a
#: push still on its way is not asked for a second time.
APPLY_GRACE_S: float = 5 * 60.0

#: A check looks at live items created in this long (the catch-up's window,
#: ``CATCHUP_MEDIA_MAX_AGE_S``); an older file is asked for on view.
SWEEP_MAX_AGE_S: int = 30 * 86_400

#: Names a check settled (a provider accepted the request, or nobody did
#: :data:`MAX_CHECK_ATTEMPTS` times), remembered so it asks each once
#: (oldest dropped). The same bound holds the names still being tried.
MAX_SWEPT_NAMES: int = 4096

#: Checks that ask for one name while no provider accepts the request.
MAX_CHECK_ATTEMPTS: int = 3

#: SQLite's naive UTC form — what ``recent_item_files`` returns rows' creation
#: times in, so they compare as text.
_SQL_TIME = "%Y-%m-%d %H:%M:%S"

#: Longest sleep of the drain task while only checks wait.
_CHECK_STEP_S: float = 1.0

#: Longest item id a provider looks up.
MAX_ITEM_ID_LEN: int = 200


class _SpaceItemFiles(Protocol):
    """``media_reference_repo``: the live space rows showing a file, and the
    files (with owners) a live item shows."""

    async def space_rows_of(self, basename: str) -> list[tuple[str, str, str]]: ...

    async def live_item_files(
        self, space_id: str, item_id: str
    ) -> list[tuple[str, str | None]]: ...

    async def recent_item_files(
        self, space_id: str, *, cutoff: str, limit: int
    ) -> list[tuple[str, str, str | None, list[str]]]: ...


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
        "_applied",
        "_apply_grace_s",
        "_asked",
        "_clock",
        "_coalesce_s",
        "_contact",
        "_dir",
        "_federation",
        "_gfs_ref",
        "_names",
        "_own",
        "_refs",
        "_seats",
        "_spaces",
        "_stop",
        "_swept",
        "_task",
        "_tries",
        "_utcnow",
    )

    def __init__(
        self,
        *,
        media_refs: _SpaceItemFiles,
        space_repo: AbstractSpaceRepo,
        remote_members: AbstractSpaceRemoteMemberRepo,
        federation: _Federation,
        host_contact: _HostContact,
        own_instance_id: str,
        clock: Callable[[], float] = time.monotonic,
        coalesce_s: float = COALESCE_S,
        media_dir: pathlib.Path | None = None,
        apply_grace_s: float = APPLY_GRACE_S,
        utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        #: Wall clock (rows carry their creation time).
        self._utcnow = utcnow
        #: name → checks that asked for it in vain (:data:`MAX_CHECK_ATTEMPTS`).
        self._tries: OrderedDict[str, int] = OrderedDict()
        #: Where this household's files are — ``None``: no check
        #: (:meth:`applied` does nothing).
        self._dir = pathlib.Path(media_dir) if media_dir is not None else None
        self._apply_grace_s = apply_grace_s
        #: space → when its check is due (:meth:`applied`).
        self._applied: dict[str, float] = {}
        #: names a check settled (:data:`MAX_SWEPT_NAMES`).
        self._swept: OrderedDict[str, None] = OrderedDict()
        #: ``name → its space_media_refs row`` (:meth:`attach_gfs_refs`).
        self._gfs_ref: Callable[[str], Awaitable[object | None]] | None = None
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
        if not _askable(basename):
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
        self._ensure_task()

    def attach_gfs_refs(self, get: Callable[[str], Awaitable[object | None]]) -> None:
        """``get(name)``: the ``space_media_refs`` row of a file this
        household reads from a GFS media cache — a check never asks for
        such a file."""
        self._gfs_ref = get

    def applied(self, space_id: str) -> None:
        """A verified event of *space_id* arrived: check its recent rows for
        files that are not here, once, :data:`APPLY_GRACE_S` from now (a
        space already marked keeps its time). O(1), never raises."""
        if (
            self._dir is None
            or not space_id
            or self._stop.is_set()
            or space_id in self._applied
            or len(self._applied) >= MAX_PENDING_NAMES
        ):
            return
        self._applied[space_id] = self._clock() + self._apply_grace_s
        try:
            self._ensure_task()
        except RuntimeError:  # no running loop
            self._applied.pop(space_id, None)

    def _ensure_task(self) -> None:
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
        self._applied.clear()
        await self.drain()

    async def _drain(self) -> None:
        while (self._names or self._applied) and not self._stop.is_set():
            wait = self._coalesce_s
            if not self._names:
                # Only checks wait: sleep until the first is due, in steps
                # short enough that a view arriving meanwhile keeps its
                # coalescing window.
                wait = max(min(self._applied.values()) - self._clock(), 0.0)
                wait = min(wait, max(self._coalesce_s, _CHECK_STEP_S))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
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
            checked: dict[str, set[str]] = {}
            for space_id in [s for s, due in self._applied.items() if due <= now]:
                del self._applied[space_id]
                try:
                    checked[space_id] = await self._check(space_id, per_space, now)
                except Exception:
                    log.warning(
                        "space media wanted: checking the files of space %s failed",
                        space_id,
                        exc_info=True,
                    )
            for space_id, files in per_space.items():
                entries = list(files.items())
                taken: set[str] = set()
                for i in range(0, len(entries), MAX_WANTED_FILES):
                    try:
                        taken |= await self._deliver(
                            space_id, entries[i : i + MAX_WANTED_FILES]
                        )
                    except Exception:
                        log.warning(
                            "space media wanted: request for space %s failed",
                            space_id,
                            exc_info=True,
                        )
                self._settle(space_id, checked.get(space_id, set()), taken, now)

    def _settle(
        self, space_id: str, checked: set[str], taken: set[str], now: float
    ) -> None:
        """After a check's requests: a name a provider accepted is settled;
        one nobody took is asked again by the space's next check (marked
        here), :data:`MAX_CHECK_ATTEMPTS` times in all — then a view asks."""
        again = False
        for name in checked:
            if name in taken:
                self._tries.pop(name, None)
                self._swept[name] = None
                continue
            # Nothing was asked: no cooldown, so a view asks at once.
            self._asked.pop(name, None)
            tries = self._tries.pop(name, 0) + 1
            if tries >= MAX_CHECK_ATTEMPTS:
                self._swept[name] = None
                log.info(
                    "space media wanted: nobody took the request for %s of space "
                    "%s in %d checks — it waits for a view",
                    name,
                    space_id,
                    tries,
                )
            else:
                self._tries[name] = tries
                again = True
        while len(self._swept) > MAX_SWEPT_NAMES:
            self._swept.popitem(last=False)
        while len(self._tries) > MAX_SWEPT_NAMES:
            self._tries.popitem(last=False)
        if again:
            self._mark(space_id, now)

    def _mark(self, space_id: str, now: float) -> None:
        """Check *space_id* again a grace from *now* (unless it is marked)."""
        if not self._stop.is_set():
            self._applied.setdefault(space_id, now + self._apply_grace_s)

    async def _check(
        self, space_id: str, per_space: dict[str, dict[str, str]], now: float
    ) -> set[str]:
        """Add to ``per_space`` the files of *space_id*'s recent live items
        that are not here (see the module docstring, "Without a view");
        the names added."""
        added: set[str] = set()
        if self._dir is None or not await holds_member_seat(self._spaces, space_id):
            return added
        wall = self._utcnow()
        cutoff = (wall - timedelta(seconds=SWEEP_MAX_AGE_S)).strftime(_SQL_TIME)
        # A row created after this may still have its push on its way.
        settled = (wall - timedelta(seconds=self._apply_grace_s)).strftime(_SQL_TIME)
        files = per_space.get(space_id, {})
        room = MAX_WANTED_FILES - len(files)
        young = False
        for created, item_id, _owner, names in await self._refs.recent_item_files(
            space_id, cutoff=cutoff, limit=MAX_WANTED_FILES
        ):
            for name in names:
                if len(added) >= room:
                    young = True  # more than one request holds: check again
                    break
                if name in files or name in self._swept or not _askable(name):
                    continue
                last = self._asked.get(name)
                if last is not None and now - last < NAME_COOLDOWN_S:
                    continue  # a view just asked
                path = media_file_path(self._dir, name)
                if path is None or await aiofiles.os.path.isfile(path):
                    continue
                if self._gfs_ref is not None and await self._gfs_ref(name) is not None:
                    continue  # read from its GFS media cache
                if created > settled:
                    young = True
                    continue
                files[name] = item_id
                added.add(name)
                self._asked[name] = now
                self._asked.move_to_end(name)
        while len(self._asked) > MAX_PENDING_NAMES:
            self._asked.popitem(last=False)
        if young:
            # Rows left out (too new, or past this check's room) get the
            # space's next check, a grace from now.
            self._mark(space_id, now)
        if added:
            per_space[space_id] = files
            log.info(
                "space media wanted: %d file(s) of space %s never arrived — "
                "asking for them",
                len(added),
                space_id,
            )
        return added

    async def _lookup(self, name: str, per_space: dict[str, dict[str, str]]) -> bool:
        """Add *name* to ``per_space`` for every space this household holds a
        member seat in whose live item shows it; whether it was added."""
        added = False
        try:
            rows = await self._refs.space_rows_of(name)
            for space_id, item_id, _created in rows:
                if name in per_space.get(space_id, {}):
                    continue
                if not await holds_member_seat(self._spaces, space_id):
                    continue  # a follower reads through its GFS (v_83)
                per_space.setdefault(space_id, {})[name] = item_id
                added = True
            if not added:
                log.info(
                    "space media wanted: %s — %s; not asked",
                    name,
                    "no live space item here shows it"
                    if not rows
                    else "no member seat in its space (a follower asks its GFS)",
                )
        except Exception:
            log.warning("space media wanted: lookup of %s failed", name, exc_info=True)
        return added

    async def _deliver(self, space_id: str, files: list[tuple[str, str]]) -> set[str]:
        """Ask the host for *files* (``(name, item)``) — the households
        acting as host here, owner first (:func:`acting_hosts`, the same
        notion the scheduler's silent-host wait uses). A helper instead only
        when the host is silent (its last sync round went unanswered — a send
        to its GFS mailbox would "succeed" with nobody to answer) or none of
        the hosts at v_92 could be reached; one batch never goes to both
        (a silent host gets it only when no helper took it). The host itself
        asks each item's author household first (it holds its own people's
        files), then a helper for the rest. Returns the names of the files
        whose request a household accepted."""
        taken: set[str] = set()
        space = await self._spaces.get(space_id)
        if space is None or not space.owner_instance_id:
            return taken
        bound = [f for f in files if is_space_direct_media_name(f[0])]
        if bound:
            # A sender-bound (legacy, unhashed) name: only the household it
            # is bound to can match it — the author's, which delivered it.
            taken |= await self._ask_authors(space_id, bound)
            files = [f for f in files if not is_space_direct_media_name(f[0])]
            if not files:
                return taken
        if space.owner_instance_id == self._own:
            # We ARE the host and lack a member's file (its push was lost):
            # ask the household of each item's author — it holds the file as
            # its own people's — then a helper for what no author took.
            taken |= await self._ask_authors(space_id, files)
            files = [f for f in files if f[0] not in taken]
            if not files:
                return taken
            hosts: list[str] = []
            silent = True
        else:
            hosts = [
                h
                for h in await acting_hosts(
                    self._spaces, self._seats, space, own_instance_id=self._own
                )
                if h and h != self._own
            ]
            silent = self._contact.is_silent(space_id)
        payload = _payload(space_id, files)
        names = {name for name, _item in files}
        if not silent:
            current = False
            for host in await self._current(hosts):
                current = True
                if await self._send(host, space_id, payload):
                    return taken | names
            if not current:
                # An older host that answers heals through its catch-up.
                return taken
        helpers = await trusted_helpers(
            self._seats,
            self._federation,
            space_id,
            exclude={*hosts, self._own},
            offset=helper_offset(self._own, space_id),
        )
        for helper in helpers[:MAX_HELPERS]:
            if await self._send(helper, space_id, payload):
                return taken | names
        if silent:
            for host in await self._current(hosts):
                if await self._send(host, space_id, payload):
                    return taken | names
        log.info(
            "space media wanted: the host of space %s is away and no helper at "
            "v_%d took %d file(s) — they wait for the catch-up",
            space_id,
            FederationCapability.MIN_FOR_SPACE_MEDIA_WANTED,
            len(files),
        )
        return taken

    async def _ask_authors(
        self, space_id: str, files: list[tuple[str, str]]
    ) -> set[str]:
        """Ask each item's author household for that item's files: ONE
        request per author household, naming only the files of its people's
        items (it may ship no others) — the first household of an item's
        author at v_92 that takes it. Returns the names a household took."""
        taken: set[str] = set()
        for households, group in (
            await self._by_author_households(space_id, files)
        ).items():
            payload = _payload(space_id, group)
            for author in households:
                if await self._federation.space_member_supports(
                    author, min_version=FederationCapability.MIN_FOR_SPACE_MEDIA_WANTED
                ) and await self._send(author, space_id, payload):
                    taken.update(name for name, _item in group)
                    break
            else:
                log.info(
                    "space media wanted: no author household of space %s took %d "
                    "file(s) (households: %s)",
                    space_id,
                    len(group),
                    ", ".join(households) or "none known",
                )
        return taken

    async def _by_author_households(
        self, space_id: str, files: list[tuple[str, str]]
    ) -> dict[tuple[str, ...], list[tuple[str, str]]]:
        """*files* grouped by the households seated for their item's author
        (``live_item_files`` owners, the roster mirror; in first-seen order,
        never us) — ``()`` for the items whose author has no known seat."""
        seats = await self._seats.list_for_space(space_id)
        groups: dict[tuple[str, ...], list[tuple[str, str]]] = {}
        for item in dict.fromkeys(item for _name, item in files):
            owners = dict.fromkeys(
                owner
                for _basename, owner in await self._refs.live_item_files(space_id, item)
                if owner
            )
            households = tuple(
                dict.fromkeys(
                    str(seat.instance_id)
                    for owner in owners
                    for seat in seats
                    if seat.user_id == owner and str(seat.instance_id) != self._own
                )
            )
            groups.setdefault(households, []).extend(f for f in files if f[1] == item)
        return groups

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
        "_own",
        "_refs",
    )

    def __init__(
        self,
        *,
        member_gate: Callable[[str, str], Awaitable[bool]],
        media_refs: _SpaceItemFiles,
        file_gates: CatchUpFileGates,
        hasher: MediaFileHasher,
        media_sync: _MediaOutbox,
        media_dir: pathlib.Path,
        budget: SpaceFileBudget,
        own_instance_id: str = "",
    ) -> None:
        #: This household — a sender-bound ``sd-`` name is ours to deliver
        #: only when it is bound to us.
        self._own = own_instance_id
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
                shipped = (
                    gate.own_only(owner, [url])
                    if is_space_direct_media_name(name)
                    else gate.ship(owner, [url])
                )
                if not shipped or size is None:
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
                    # A content name is sent as asked (a helper may store it
                    # under its own reader name, ``sg-``, which the requester
                    # refuses by name); a sender-bound one under the file's
                    # own name, which the requester binds to us again.
                    wire_names=None
                    if is_space_direct_media_name(name)
                    else {url: name},
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
        bound = is_space_direct_media_name(name)
        for basename, owner in await self._refs.live_item_files(space_id, item_id):
            if basename == name:
                return basename, owner
            if bound:
                if self._own and (
                    space_direct_media_name(space_id, self._own, basename) == name
                ):
                    return basename, owner
                continue
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
    if not isinstance(name, str) or not _askable(name):
        return None
    return item_id, name


def _askable(name: str) -> bool:
    """A name a request may carry: a content name (``sc-``) or a
    sender-bound one (``sd-``), and a safe media name."""
    return (
        is_space_content_media_name(name) or is_space_direct_media_name(name)
    ) and is_safe_media_name(name)


def _payload(space_id: str, files: list[tuple[str, str]]) -> dict:
    return {
        "space_id": space_id,
        "files": [{"item": item, "name": name} for name, item in files],
    }


__all__ = [
    "APPLY_GRACE_S",
    "COALESCE_S",
    "MAX_HELPERS",
    "MAX_WANTED_FILES",
    "NAME_COOLDOWN_S",
    "SpaceMediaWanted",
    "SpaceMediaWantedProvider",
]
