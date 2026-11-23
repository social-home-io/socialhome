"""A space's member and follower counts — shown, and re-shared on change.

Owner decision (2026-11-23): the counts are part of a space's shared
listing metadata (:mod:`socialhome.domain.space_listing_counts`). The host
declares them in its signed GFS listing and in the paired-household
directory snapshot, both built by ``space_repo.listing_counts``.

This service does the two things around that builder:

* **Shows** the counts on the space page (:meth:`shown_counts`): the
  roster's own counts when this household holds the roster (one of its
  people holds a member seat), else the counts the host shared (the GFS
  directory cache for a global space, the paired directory for a public
  one), else no number — never a made-up 0.
* **Re-shares** them when they change: a join, leave, follow, unfollow or
  kick in a global or public space this household hosts marks the space;
  a ban or a role change (a config change) does too; one trailing timer per space (:data:`REPUBLISH_DEBOUNCE_S`) then reads
  the counts once and, if they differ from what it last shared, re-publishes
  the GFS listing (to the servers already listing it) and the paired
  directory snapshot. A burst of joins is one re-share; a quiet space sends
  nothing. A change still waiting when the household stops is shared by
  the next change.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Protocol

from ..domain.events import (
    SpaceConfigChanged,
    SpaceMemberJoined,
    SpaceMemberLeft,
    SpaceRemoteSeatEnded,
    SpaceRemoteSeatLive,
)
from ..domain.space import Space, SpaceType
from ..domain.space_listing_counts import SpaceListingCounts

if TYPE_CHECKING:
    from ..infrastructure.event_bus import EventBus
    from ..repositories.peer_space_directory_repo import (
        AbstractPeerSpaceDirectoryRepo,
    )
    from ..repositories.public_space_repo import AbstractPublicSpaceRepo
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)

__all__ = ["REPUBLISH_DEBOUNCE_S", "SpaceListingCountsService"]

#: At most one count re-share per space in this window (seconds).
REPUBLISH_DEBOUNCE_S: float = 180.0

#: The tiers whose listing carries counts: a global space is listed on GFSs,
#: a public one in the paired-household directory.
_LISTED_TIERS = frozenset({SpaceType.GLOBAL, SpaceType.PUBLIC})


class GfsListingRepublisher(Protocol):
    async def republish_space(self, space_id: str, *, announce: bool = True) -> int: ...


class PeerDirectoryRefresher(Protocol):
    async def refresh(self) -> None: ...


class SpaceListingCountsService:
    """Show a space's listing counts and re-share them, coalesced, on change."""

    __slots__ = (
        "_bus",
        "_debounce_s",
        "_gfs",
        "_last",
        "_own_instance_id",
        "_peer_directory",
        "_peer_directory_repo",
        "_public_space_repo",
        "_space_repo",
        "_tasks",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        space_repo: AbstractSpaceRepo,
        public_space_repo: AbstractPublicSpaceRepo,
        peer_space_directory_repo: AbstractPeerSpaceDirectoryRepo,
        gfs: GfsListingRepublisher,
        debounce_s: float = REPUBLISH_DEBOUNCE_S,
    ) -> None:
        self._bus = bus
        self._space_repo = space_repo
        self._public_space_repo = public_space_repo
        self._peer_directory_repo = peer_space_directory_repo
        self._gfs = gfs
        self._debounce_s = debounce_s
        #: Set once federation is up (:meth:`attach`); until then nothing is
        #: re-shared — there is nobody to share with.
        self._peer_directory: PeerDirectoryRefresher | None = None
        self._own_instance_id = ""
        #: space id → the counts it last shared (RAM: after a restart the
        #: next change shares once). Bounded by the spaces hosted here.
        self._last: dict[str, SpaceListingCounts] = {}
        #: space id → its pending trailing timer.
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def attach(
        self, *, peer_directory: PeerDirectoryRefresher, own_instance_id: str
    ) -> None:
        """Federation is up: re-share through these from now on."""
        self._peer_directory = peer_directory
        self._own_instance_id = own_instance_id

    def wire(self) -> None:
        # A local seat (join, leave, follow, unfollow, kick) or a remote one.
        self._bus.subscribe(SpaceMemberJoined, self._on_seat_changed)
        self._bus.subscribe(SpaceMemberLeft, self._on_seat_changed)
        self._bus.subscribe(SpaceRemoteSeatLive, self._on_seat_changed)
        self._bus.subscribe(SpaceRemoteSeatEnded, self._on_seat_changed)
        # A ban or a role change (member ↔ follower) is a config change; the
        # flush re-shares only if the counts really moved.
        self._bus.subscribe(SpaceConfigChanged, self._on_seat_changed)

    async def stop(self) -> None:
        """Drop every pending timer (the next change re-shares)."""
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ── Showing ────────────────────────────────────────────────────────

    async def shown_counts(self, space: Space) -> tuple[int | None, int | None]:
        """``(members, followers)`` for the space page; ``None`` = no number.

        The roster's own counts when this household holds it, else what the
        host shared with this household (an older host shares fewer — those
        stay ``None``)."""
        if space.space_type not in _LISTED_TIERS or await self._space_repo.holds_roster(
            space.id
        ):
            counts = await self._space_repo.listing_counts(space.id)
            if counts is None:
                return None, None
            return counts.member_count, counts.follower_count
        if space.space_type is SpaceType.GLOBAL:
            listing = await self._public_space_repo.get(space.id)
            if listing is None:
                return None, None
            return listing.member_count, listing.follower_count
        entry = await self._peer_directory_repo.get(space.owner_instance_id, space.id)
        if entry is None:
            return None, None
        return entry.member_count, entry.follower_count

    # ── Re-sharing ─────────────────────────────────────────────────────

    async def _on_seat_changed(
        self,
        event: SpaceConfigChanged
        | SpaceMemberJoined
        | SpaceMemberLeft
        | SpaceRemoteSeatLive
        | SpaceRemoteSeatEnded,
    ) -> None:
        space_id = event.space_id
        if space_id in self._tasks or not await self._hosted_listing(space_id):
            return
        self._tasks[space_id] = asyncio.create_task(
            self._flush_later(space_id), name=f"listing-counts-{space_id}"
        )

    async def _hosted_listing(self, space_id: str) -> bool:
        """A global or public space this household hosts."""
        if self._peer_directory is None or not self._own_instance_id:
            return False
        space = await self._space_repo.get(space_id)
        return (
            space is not None
            and not space.dissolved
            and space.space_type in _LISTED_TIERS
            and space.owner_instance_id == self._own_instance_id
        )

    async def _flush_later(self, space_id: str) -> None:
        try:
            await asyncio.sleep(self._debounce_s)
        except asyncio.CancelledError:
            return
        self._tasks.pop(space_id, None)
        try:
            await self.flush(space_id)
        except Exception:  # never kill the loop: the next change retries
            log.warning(
                "space %s: re-sharing its listing counts failed",
                space_id,
                exc_info=True,
            )

    async def flush(self, space_id: str) -> None:
        """Re-share *space_id*'s counts now if they changed since last time."""
        space = await self._space_repo.get(space_id)
        counts = await self._space_repo.listing_counts(space_id)
        if space is None or counts is None or self._last.get(space_id) == counts:
            return
        if space.space_type is SpaceType.GLOBAL:
            # Only the servers already listing it; the members' GFS list
            # does not change with a count, so it is not re-announced. No
            # server took it (none lists it, or all were down): not marked
            # shared, so the next change tries again.
            if not await self._gfs.republish_space(space_id, announce=False):
                return
        elif self._peer_directory is not None:
            # Per peer, only when what that peer would get changed.
            await self._peer_directory.refresh()
        self._last[space_id] = counts
