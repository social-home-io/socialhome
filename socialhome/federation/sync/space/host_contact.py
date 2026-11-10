"""When this household last heard a space's host (§25.6, v_92 helpers).

The receiver marks a space *heard* whenever the household acting as its
host here (the owner, or the delegated admin household that invited us —
``receiver._acts_as_host``) answers our ``SPACE_SYNC_BEGIN``: any signed
sync chunk of that space we verify — the ``__complete__`` sentinel of an
up-to-date empty incremental session included, which is all a quiet space
sends back — and the scheduler marks it on a host's "no free slot" deferral.
The scheduler reads it a fixed while after a BEGIN to the host: no answer
since then means the host is offline (or has no path to us), and ONE
trusted helper is asked for the bounded ``SPACE_SYNC_RESUME`` instead
(``scheduler.SpaceSyncScheduler``).

In memory only: a restart starts every space silent from the construction
time, so a household that just booted waits a full window before it falls
back. Bounded: at most :data:`MAX_SPACES_TRACKED` spaces, oldest dropped.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime

#: Spaces remembered (oldest heard dropped first).
MAX_SPACES_TRACKED: int = 4096


def _utc_now() -> datetime:
    return datetime.now(UTC)


class HostContact:
    """Per space: the monotonic and the UTC wall-clock time its host was
    last heard."""

    __slots__ = ("_clock", "_heard", "_since", "_wall")

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._clock = clock
        self._wall = wall
        self._since = clock()
        self._heard: OrderedDict[str, tuple[float, str]] = OrderedDict()

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

    def heard_since(self, space_id: str, t: float) -> bool:
        """Whether the host answered at or after *t* (:meth:`now`)."""
        held = self._heard.get(space_id)
        return held is not None and held[0] >= t

    def silent_for(self, space_id: str) -> float:
        """Seconds since the host was last heard — since this household
        started, when it has not been heard yet."""
        held = self._heard.get(space_id)
        return self._clock() - (held[0] if held is not None else self._since)

    def last_heard_iso(self, space_id: str) -> str | None:
        """When the host was last heard (UTC ISO 8601); ``None``: not yet."""
        held = self._heard.get(space_id)
        return held[1] if held is not None else None
