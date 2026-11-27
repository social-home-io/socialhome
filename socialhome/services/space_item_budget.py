"""Sender budgets for a GFS space's channel items (v_97).

Per PERSON and space (``docs/protocol/space-items.md`` › "Sender budgets
and location"), sized so an active space of 10 000 seats runs without a
throttle anyone notices, and so one household can't flood the channel:

* **chat** — a burst of :data:`CHAT_BURST` messages, then
  :data:`CHAT_PER_SECOND` a second sustained (a token bucket); the rest wait
  in order;
* **location** — at most one pin per :data:`LOCATION_INTERVAL_S`, latest
  only (the durable queue replaces a pin still waiting by the newer one);
* **content** — never held here (the GFS's own publish budgets and the
  person's own pace bound it).

The budget only says WHEN an item may go (:meth:`SenderBudget.delay`): the
item waits for its slot in the durable queue
(:mod:`socialhome.services.space_item_outbox`), never in memory, so a
restart loses nothing. The buckets themselves are in memory: a restart
starts everyone with a full burst.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from ..domain.space_item import BUDGET_CHAT, BUDGET_LOCATION

#: Chat messages a person may send at once before pacing starts.
CHAT_BURST: int = 10
#: Sustained chat messages per person and second.
CHAT_PER_SECOND: float = 1.0
#: One location pin per person per this many seconds (latest only).
LOCATION_INTERVAL_S: float = 10.0
#: People tracked before idle buckets are forgotten (a forgotten bucket is
#: a full one — exactly what an idle person's bucket holds anyway).
MAX_TRACKED: int = 10_000


class SenderBudget:
    """When a person's next chat / location item may go (see the module
    docstring)."""

    __slots__ = ("_clock", "_pins", "_tokens")

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        #: (space, person) → (tokens, when last refilled); tokens go
        #: negative for messages already promised a later slot.
        self._tokens: dict[tuple[str, str], tuple[float, float]] = {}
        #: (space, person) → the slot of the last pin scheduled.
        self._pins: dict[tuple[str, str], float] = {}

    def delay(self, space_id: str, budget: str, person: str) -> float:
        """Seconds the item must wait for its slot (``0.0``: now). Taking
        the slot is part of the call: ask once per item."""
        key = (space_id, person)
        if budget == BUDGET_CHAT:
            return self._chat(key)
        if budget == BUDGET_LOCATION:
            return self._pin(key)
        return 0.0

    def _chat(self, key: tuple[str, str]) -> float:
        now = self._clock()
        tokens, at = self._tokens.get(key, (float(CHAT_BURST), now))
        tokens = min(float(CHAT_BURST), tokens + (now - at) * CHAT_PER_SECOND) - 1.0
        self._tokens[key] = (tokens, now)
        self._prune(self._tokens, now)
        return 0.0 if tokens >= 0.0 else -tokens / CHAT_PER_SECOND

    def _pin(self, key: tuple[str, str]) -> float:
        now = self._clock()
        last = self._pins.get(key)
        if last is not None and last > now:
            # A pin still waits for that slot; the newer one replaces it in
            # the queue and takes its slot over.
            due = last
        else:
            due = now if last is None else max(now, last + LOCATION_INTERVAL_S)
        self._pins[key] = due
        self._prune(self._pins, now)
        return due - now

    def _prune(self, table: dict, now: float) -> None:
        if len(table) <= MAX_TRACKED:
            return
        if table is self._tokens:
            for k in [
                k
                for k, (tokens, at) in self._tokens.items()
                if tokens + (now - at) * CHAT_PER_SECOND >= CHAT_BURST
            ]:
                del self._tokens[k]
        else:
            for k in [
                k for k, at in self._pins.items() if at + LOCATION_INTERVAL_S <= now
            ]:
                del self._pins[k]
