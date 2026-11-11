"""When this household last heard a space's host (§25.6, v_92 helpers).

The receiver marks a space *heard* whenever the household acting as its
host here (the owner, or the delegated admin household that invited us —
``receiver._acts_as_host``) answers our ``SPACE_SYNC_BEGIN``: any signed
sync chunk of that space we verify — the ``__complete__`` sentinel of an
up-to-date empty incremental session included, which is all a quiet space
sends back — and the scheduler marks it on any signed refusal of our BEGIN by
the host (no free slot, the hourly budget spent).
The scheduler reads it a fixed while after a BEGIN to the host: no answer
since then means the host is offline (or has no path to us), and ONE
trusted helper is asked for the bounded ``SPACE_SYNC_RESUME`` instead
(``scheduler.SpaceSyncScheduler``).

It also holds whether the host is **silent** now: the scheduler marks a
space when a round's BEGIN to the households acting as its host went
unanswered for the full wait (:meth:`HostContact.went_silent`, which keeps
those households). Their next sync answer clears it — and so does ANY
verified event one of them sends for that space (a post, a config change;
``FederationService._dispatch_event`` → :meth:`HostContact.heard_from`), so
a host that comes back stops helper escalation at once, not only on the
next 30-minute round. A member asks a trusted helper for a file it misses
(``SPACE_MEDIA_WANTED``) only while the host is silent or cannot be reached
(``services.space_media_wanted``) — never while it answers.

In memory only: a restart forgets every answer, so a household that just
booted falls back only once a BEGIN it sends goes unanswered for the full
window, and is not silent before that. Where a helper resume starts is not
kept here: it comes from the rows this household stored before this process
started (the scheduler's boot row marks, ``scheduler._resume_since``), so it
survives a restart and a row landing after the restart cannot move it past
the gap. Bounded: at most :data:`MAX_SPACES_TRACKED` spaces per map,
oldest dropped.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

#: Spaces remembered (oldest heard dropped first).
MAX_SPACES_TRACKED: int = 4096


def _utc_now() -> datetime:
    return datetime.now(UTC)


class HostContact:
    """Per space: the monotonic and the UTC wall-clock time its host was
    last heard."""

    __slots__ = ("_clock", "_heard", "_silent", "_wall")

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._clock = clock
        self._wall = wall
        self._heard: OrderedDict[str, tuple[float, str]] = OrderedDict()
        #: spaces whose host left a round unanswered → the households that
        #: acted as its host for that round (oldest first).
        self._silent: OrderedDict[str, frozenset[str]] = OrderedDict()

    def now(self) -> float:
        """The clock :meth:`heard_since` compares against."""
        return self._clock()

    def heard(self, space_id: str) -> None:
        """The household acting as *space_id*'s host answered us."""
        if not space_id:
            return
        self._heard[space_id] = (self._clock(), self._wall().isoformat())
        self._heard.move_to_end(space_id)
        while len(self._heard) > MAX_SPACES_TRACKED:
            self._heard.popitem(last=False)
        self._silent.pop(space_id, None)

    def heard_since(self, space_id: str, t: float) -> bool:
        """Whether the host answered at or after *t* (:meth:`now`)."""
        held = self._heard.get(space_id)
        return held is not None and held[0] >= t

    def last_heard_iso(self, space_id: str) -> str | None:
        """When the host was last heard (UTC ISO 8601); ``None``: not yet."""
        held = self._heard.get(space_id)
        return held[1] if held is not None else None

    def went_silent(self, space_id: str, hosts: Iterable[str]) -> None:
        """*hosts* — the households acting as *space_id*'s host here — left
        this round's BEGIN unanswered."""
        if not space_id:
            return
        self._silent[space_id] = frozenset(h for h in hosts if h)
        self._silent.move_to_end(space_id)
        while len(self._silent) > MAX_SPACES_TRACKED:
            self._silent.popitem(last=False)

    def heard_from(self, space_id: str, instance_id: str) -> None:
        """A verified event of *space_id* arrived from *instance_id*: if it
        is one of the households the silent round waited on, the host is
        back. Never moves :meth:`last_heard_iso` — only a sync answer says
        what the host covered. In memory, O(1): called for every event."""
        hosts = self._silent.get(space_id) if space_id else None
        if hosts is not None and instance_id in hosts:
            del self._silent[space_id]

    def is_silent(self, space_id: str) -> bool:
        """Whether *space_id*'s host left its last round unanswered and has
        not been heard since."""
        return space_id in self._silent
