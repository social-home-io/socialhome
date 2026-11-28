"""A space file for ONE reader, straight from a household that holds it.

Owner 2026-11-28 (media routing): new content that goes to many households
is uploaded once to the GFS space media cache and read from there. A file
only one or a few readers want — a catch-up's old file, a newcomer's
gallery sync, a file past the cache's lifetime, the host archive catching
up, a missed file on view after the cache let it go — is fetched DIRECTLY
from a household that holds it, never re-uploaded into the cache for one
reader. Order for such a fetch (the caller, ``SpaceMediaGfsFetcher``):
direct link → the GFS cache if it still holds it → v_83 on demand (which
streams old content through, storing nothing).

**Direct.** :meth:`SpaceMediaDirect.fetch` asks the households that can
ship the file (the ``SPACE_MEDIA_WANTED`` provider's file gate: the
households acting as the space's host — owner first — then the household
that delivered the item, which holds its own people's files) one at a
time, at most :data:`MAX_DIRECT_HOLDERS`:

1. a direct WebRTC link to it — the one a paired household already has,
   or one opened now (:meth:`FederationService.open_direct_link`). Since
   owner 2026-11-28 households that met only through a GFS may hold one
   too (both see each other's IP; standard behaviour, no setting): its
   offer / answer / ICE travel as ordinary sealed federation events through
   the GFS relay, and each side takes them only from a household seated
   with it in a shared space (v_97);
2. one ``SPACE_MEDIA_WANTED`` for the file (v_92, unchanged) over that
   link; the holder's ``SPACE_MEDIA_BLOB`` comes back as binary frames on
   ``fed-media-v1`` and the receiver checks the bytes against the content
   name before storing them (``sc-``: the one name every path agrees on —
   a file without one is never asked for);
3. the file is waited for at most :data:`DIRECT_FILE_WAIT_S`.

A holder whose link does not come up (NAT without TURN, offline, older than
v_97) is skipped; when none delivers, the caller goes on to the GFS cache.

**Bounds.** At most :data:`MAX_DIRECT_AT_ONCE` direct fetches at once and
one per space; the holder's provider budget (50 files / 32 MiB per
requester and space per 30 minutes) bounds what one household can pull. A
link this service opened to a household met through a GFS is closed again
once idle for :data:`LINK_IDLE_S`, and at most :data:`MAX_SEAT_LINKS` such
links are kept (the least recently used goes first).

**Cost.** Per file: one link setup when none is up (a few relayed
signalling envelopes, ~2–6 KiB through the GFS mailbox), a < 1 KiB request,
and the file's bytes once, household to household — nothing stored on the
GFS. A newcomer's 2 GiB gallery from a host: ~2 GiB once over one link,
paced by the provider budget (32 MiB / 30 min per holder).
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Protocol

import aiofiles.os

from ..domain.federation import FederationEventType
from ..domain.file_hashes import is_space_content_media_name
from ..federation.host_equivalence import acting_hosts
from .inbound_media_store import media_file_path

if TYPE_CHECKING:
    from ..domain.space_media_ref import SpaceMediaRef
    from ..repositories.space_remote_member_repo import AbstractSpaceRemoteMemberRepo
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)

#: How long a link may take to come up, and the file to arrive over it.
DIRECT_LINK_WAIT_S: float = 20.0
DIRECT_FILE_WAIT_S: float = 120.0
DIRECT_POLL_S: float = 0.5
#: Holders asked per file, direct fetches at once (one per space).
MAX_DIRECT_HOLDERS: int = 3
MAX_DIRECT_AT_ONCE: int = 2
#: A link opened here to a household met through a GFS: closed once idle
#: this long; at most this many kept.
LINK_IDLE_S: float = 600.0
MAX_SEAT_LINKS: int = 8


class _Result(Protocol):
    @property
    def ok(self) -> bool: ...


class _Federation(Protocol):
    @property
    def own_instance_id(self) -> str: ...

    async def open_direct_link(self, instance_id: str, *, timeout_s: float) -> bool: ...

    async def close_seat_link(self, instance_id: str) -> None: ...

    async def send_event(
        self,
        *,
        to_instance_id: str,
        event_type: FederationEventType,
        payload: dict,
        space_id: str | None = None,
    ) -> _Result: ...


async def shares_a_seated_space(
    spaces: AbstractSpaceRepo,
    seats: AbstractSpaceRemoteMemberRepo,
    instance_id: str,
) -> bool:
    """Whether household *instance_id* holds a live seat in a space a local
    person holds a seat in — the one condition for a direct link between
    households met through a GFS (v_97). Fail closed."""
    try:
        for space in await spaces.list_all():
            if not await seats.list_for_instance(
                space.id, instance_id, include_tombstoned=False
            ):
                continue
            if await spaces.list_local_member_user_ids(space.id):
                return True
    except Exception:
        log.warning("seats shared with %s not read", instance_id, exc_info=True)
    return False


class SpaceMediaDirect:
    """Fetch one file of a space from a household that holds it."""

    __slots__ = (
        "_dir",
        "_fed",
        "_opened",
        "_running",
        "_seats",
        "_sem",
        "_spaces",
    )

    def __init__(
        self,
        *,
        federation: _Federation,
        spaces: AbstractSpaceRepo,
        seats: AbstractSpaceRemoteMemberRepo | None,
        media_dir: pathlib.Path,
    ) -> None:
        self._fed = federation
        self._spaces = spaces
        self._seats = seats
        self._dir = pathlib.Path(media_dir)
        self._sem = asyncio.Semaphore(MAX_DIRECT_AT_ONCE)
        #: Spaces with a direct fetch running (one each).
        self._running: dict[str, asyncio.Lock] = {}
        #: household → monotonic time this service last used its link.
        self._opened: OrderedDict[str, float] = OrderedDict()

    async def fetch(self, ref: SpaceMediaRef) -> bool:
        """Whether *ref*'s file is here now, fetched from a holder over a
        direct link. Never raises."""
        path = media_file_path(self._dir, ref.local_name)
        if path is None or not is_space_content_media_name(ref.local_name):
            return False  # asked by its content name only
        try:
            holders = await self._holders(ref)
        except Exception:
            log.warning(
                "space media %s: holders not read", ref.local_name, exc_info=True
            )
            return False
        # One per space (a lock per space this household reads directly).
        lock = self._running.setdefault(ref.space_id, asyncio.Lock())
        try:
            async with self._sem, lock:
                for holder in holders[:MAX_DIRECT_HOLDERS]:
                    if await self._from(holder, ref, path):
                        return True
                return False
        except Exception:
            log.warning(
                "space media %s: direct fetch failed", ref.local_name, exc_info=True
            )
            return False
        finally:
            await self._close_idle()

    async def _holders(self, ref: SpaceMediaRef) -> list[str]:
        """Who may ship it: the acting hosts (owner first), then the
        household that delivered the item."""
        own = self._fed.own_instance_id
        space = await self._spaces.get(ref.space_id)
        found: list[str] = []
        if space is not None:
            found.extend(
                await acting_hosts(
                    self._spaces, self._seats, space, own_instance_id=own
                )
            )
        found.append(ref.origin)
        out: list[str] = []
        for iid in found:
            if iid and iid != own and iid not in out:
                out.append(iid)
        return out

    async def _from(self, holder: str, ref: SpaceMediaRef, path: pathlib.Path) -> bool:
        if not await self._fed.open_direct_link(holder, timeout_s=DIRECT_LINK_WAIT_S):
            log.info(
                "space media %s: no direct link to %s — next holder",
                ref.local_name,
                holder,
            )
            return False
        self._opened[holder] = time.monotonic()
        self._opened.move_to_end(holder)
        result = await self._fed.send_event(
            to_instance_id=holder,
            event_type=FederationEventType.SPACE_MEDIA_WANTED,
            payload={
                "space_id": ref.space_id,
                "files": [{"item": ref.item_id, "name": ref.local_name}],
            },
            space_id=ref.space_id,
        )
        if not result.ok:
            return False
        deadline = time.monotonic() + DIRECT_FILE_WAIT_S
        while time.monotonic() < deadline:
            if await aiofiles.os.path.isfile(path):
                self._opened[holder] = time.monotonic()
                log.info(
                    "space media %s (%s of %s): fetched directly from %s",
                    ref.local_name,
                    ref.filename,
                    ref.item_id,
                    holder,
                )
                return True
            await asyncio.sleep(DIRECT_POLL_S)
        log.info(
            "space media %s: %s did not deliver it in %.0f s",
            ref.local_name,
            holder,
            DIRECT_FILE_WAIT_S,
        )
        return False

    async def _close_idle(self) -> None:
        """Close the links this service opened that sat idle, and the
        oldest past :data:`MAX_SEAT_LINKS` (a paired household's link is
        never closed here: :meth:`close_seat_link` leaves it)."""
        now = time.monotonic()
        while self._opened:
            holder, used = next(iter(self._opened.items()))
            if now - used < LINK_IDLE_S and len(self._opened) <= MAX_SEAT_LINKS:
                break
            self._opened.popitem(last=False)
            try:
                await self._fed.close_seat_link(holder)
            except Exception:
                log.debug("link to %s not closed", holder, exc_info=True)


__all__ = [
    "DIRECT_FILE_WAIT_S",
    "DIRECT_LINK_WAIT_S",
    "LINK_IDLE_S",
    "MAX_DIRECT_AT_ONCE",
    "MAX_DIRECT_HOLDERS",
    "MAX_SEAT_LINKS",
    "SpaceMediaDirect",
    "shares_a_seated_space",
]
