"""Space fan-outs a just-joined household made before the host's roster came.

A household that accepts a remote invitation is seated from the roster the
invitation carried — taken when the invitation was built, so it lacks every
household seated since (with several invitations sent at once: everyone but
the host). The host sends its whole roster (``SPACE_ROSTER_SNAPSHOT``) as it
seats the household, but until that lands :meth:`FederationService
.broadcast_to_space_members` here reaches only the households that stale
roster lists. A post made in that window reached the other members only
through their own §25.6 catch-up — at worst the 30-minute periodic tick —
so for minutes a newcomer's first post existed on the host and nowhere else.

:class:`PendingRosterFanout` closes the window without holding anything
back. From the accept until the host's whole, verified snapshot is applied,
every fan-out to that space is remembered with the member households it was
planned for; then each goes once more — through the same
``broadcast_to_space_members`` gates — to exactly the member households the
roster added. Nobody gets an item twice, and the total sent is what a
complete roster would have sent at the time.

All or nothing: when a bound refuses one fan-out, the space's whole wait is
dropped — replaying a post without the later delete that was not kept would
bring deleted content back. A fan-out narrowed to some households
(``only_instances``: space chat, zones, live location) is not kept: its
caller chose the audience from the same stale roster, so the added
households are outside it anyway; those reach them through the catch-up,
as before.

Memory only and bounded — spaces waiting, fan-outs per space, bytes, and a
TTL: a household that just joined makes a handful of writes before its
roster arrives (well under a second normally, seconds over a slow path). A
restart, a host that never sends a snapshot (pre-v_32; an invitation from
a delegated admin, which starts no wait: only the owner sends one) or a
bound reached falls back to the §25.6 catch-up, the behaviour before this existed.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..domain.federation import FederationEventType
from .bounded_hold import BoundedHold, HoldKey

#: How long a just-joined household waits for the host's roster. The host
#: sends it as it seats the household; over a durable outbox ladder
#: (5/10/20/40 s) or a mesh route rebuilt after a restart it may take a few
#: minutes, never longer than the route-cache window.
ROSTER_WAIT_TTL_S: float = 300.0

#: Spaces joined at once that may wait on their roster.
MAX_WAITING_SPACES: int = 16

#: Fan-outs remembered per waiting space.
MAX_FANOUTS_PER_SPACE: int = 64

#: Payload bytes remembered over all waiting spaces.
MAX_FANOUT_BYTES: int = 8 * 1024 * 1024

_KIND = "roster-wait"


@dataclass(slots=True, frozen=True)
class PendingFanout:
    """One ``broadcast_to_space_members`` call, with the member households
    it was planned for (memory only; never stored or sent). ``payload`` is
    held as given: callers build a fresh dict per broadcast.

    ``per_peer`` is called again when the fan-out is resent — the hooks
    that use it decide per household at call time, which stays right."""

    event_type: FederationEventType
    payload: dict
    #: Every member household the roster listed when it was sent.
    reached: frozenset[str]
    min_proto_version: int | None = None
    legacy_payload: dict | None = None
    legacy_below: int | None = None
    per_peer: Callable[[str, dict], Awaitable[dict]] | None = None


class PendingRosterFanout:
    """Fan-outs to spaces whose roster this household still waits for."""

    __slots__ = ("_clock", "_hold", "_max_spaces", "_ttl", "_waiting")

    def __init__(
        self,
        *,
        ttl_seconds: float = ROSTER_WAIT_TTL_S,
        max_spaces: int = MAX_WAITING_SPACES,
        max_per_space: int = MAX_FANOUTS_PER_SPACE,
        max_bytes: int = MAX_FANOUT_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_spaces = max_spaces
        self._clock = clock
        #: ``space_id`` → when the wait ends (``clock`` time).
        self._waiting: dict[str, float] = {}
        self._hold: BoundedHold[PendingFanout] = BoundedHold(
            ttl_seconds=ttl_seconds,
            max_entries=max_spaces * max_per_space,
            max_per_key=max_per_space,
            max_per_sender=max_per_space,
            max_bytes=max_bytes,
            clock=clock,
        )

    def _purge(self) -> None:
        now = self._clock()
        for space_id in [s for s, end in self._waiting.items() if end <= now]:
            self.end(space_id)

    def expect(self, space_id: str) -> None:
        """This household is joining ``space_id``: remember its fan-outs
        until :meth:`end`. With every slot taken, the space whose wait ends
        first gives way."""
        self._purge()
        if space_id not in self._waiting and len(self._waiting) >= self._max_spaces:
            self.end(min(self._waiting, key=self._waiting.__getitem__))
        self._waiting[space_id] = self._clock() + self._ttl

    def waiting(self, space_id: str) -> bool:
        """Whether a fan-out to ``space_id`` is to be remembered."""
        self._purge()
        return space_id in self._waiting

    def record(self, space_id: str, fanout: PendingFanout, *, size: int) -> bool:
        """Remember ``fanout`` (``size`` payload bytes) while ``space_id``
        waits. ``False`` when it does not wait — or a bound refused it, which
        ends the whole wait (all or nothing, see the module doc)."""
        if not self.waiting(space_id):
            return False
        if self._hold.hold(
            self._key(space_id),
            fanout,
            token=object(),
            sender=space_id,
            size=size,
        ):
            return True
        self.end(space_id)
        return False

    def take(self, space_id: str) -> list[PendingFanout]:
        """Hand back what was fanned out to ``space_id`` so far, oldest
        first; the wait goes on (a fan-out made while these are resent is
        remembered for the next take)."""
        if not self.waiting(space_id):
            return []
        return self._hold.release(self._key(space_id))

    def end(self, space_id: str) -> None:
        """Stop waiting for ``space_id`` and forget what was kept for it."""
        self._waiting.pop(space_id, None)
        self._hold.release(self._key(space_id))

    @staticmethod
    def _key(space_id: str) -> HoldKey:
        return (space_id, _KIND, "")
