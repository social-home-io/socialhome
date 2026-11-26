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
complete roster would have sent at the time. While the space waits, a
fan-out goes live only to the households the first kept one reached
(:meth:`PendingRosterFanout.audience`): one the roster gains meanwhile gets
every kept fan-out from the resend, in order, so a delete never reaches it
before its post — and no write waits for the resend.

All or nothing: when a bound refuses one fan-out, the space's whole wait is
dropped — replaying a post without the later delete that was not kept would
bring deleted content back. A fan-out narrowed to some households
(``only_instances``: space chat, zones, live location) is not kept: its
caller chose the audience from the same stale roster, so the added
households are outside it anyway; those reach them through the catch-up,
as before.

Memory only and bounded — spaces waiting, fan-outs per space, bytes, and a
deadline: a household that just joined makes a handful of writes before its
roster arrives (well under a second normally, seconds over a slow path).
When the snapshot does not come by :data:`ROSTER_WAIT_TTL_S` (a lost part,
a host before v_32), the federation service resends anyway, to the
households the roster gained by then — the wait never just drops what it
kept. A restart, a 17th space joined at once (it starts no wait) or a bound
reached falls back to the §25.6 catch-up, the behaviour before this
existed. An invitation from a delegated admin starts no wait: only the
owner sends a snapshot.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..domain.federation import FederationEventType
from .bounded_hold import BoundedHold, HoldKey

#: How long a just-joined household waits for the host's roster before it
#: resends what it kept anyway. The host sends it as it seats the household;
#: over a durable outbox ladder (5/10/20/40 s) or a mesh route rebuilt after
#: a restart it may take a few minutes, never longer than the route-cache
#: window.
ROSTER_WAIT_TTL_S: float = 300.0

#: Backstop: a wait nobody ended (no deadline armed) is forgotten after this.
ROSTER_WAIT_BACKSTOP_S: float = 2 * ROSTER_WAIT_TTL_S

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
    #: The member households it was planned for live (a gate may still have
    #: skipped one); the resend goes to the others only.
    reached: frozenset[str]
    min_proto_version: int | None = None
    legacy_payload: dict | None = None
    legacy_below: int | None = None
    per_peer: Callable[[str, dict], Awaitable[dict]] | None = None


class PendingRosterFanout:
    """Fan-outs to spaces whose roster this household still waits for."""

    __slots__ = ("_audience", "_clock", "_hold", "_max_spaces", "_ttl", "_waiting")

    def __init__(
        self,
        *,
        ttl_seconds: float = ROSTER_WAIT_BACKSTOP_S,
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
        #: ``space_id`` → the member households the first kept fan-out
        #: reached (:meth:`audience`).
        self._audience: dict[str, frozenset[str]] = {}
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

    def expect(self, space_id: str) -> bool:
        """This household is joining ``space_id``: remember its fan-outs
        until :meth:`end`. ``True`` when this started the wait — not when
        the space already waits, nor when every slot is taken (that join
        starts none: a wait under way is never cut short)."""
        self._purge()
        if space_id in self._waiting or len(self._waiting) >= self._max_spaces:
            return False
        self._waiting[space_id] = self._clock() + self._ttl
        return True

    def audience(self, space_id: str, members: frozenset[str]) -> frozenset[str]:
        """Who a fan-out to waiting ``space_id`` goes to live, of the
        current ``members``: those every earlier call reached (pinned by
        the first call, and a household that drops out stays out, even
        when it comes back). A household the roster gains meanwhile gets
        every kept fan-out from the resend, in order — never a later one
        live before an earlier one resent."""
        pinned = self._audience.get(space_id, members) & members
        self._audience[space_id] = pinned
        return pinned

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
        self._audience.pop(space_id, None)
        self._hold.release(self._key(space_id))

    @staticmethod
    def _key(space_id: str) -> HoldKey:
        return (space_id, _KIND, "")
