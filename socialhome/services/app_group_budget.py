"""The send budget of one group table (design 2.12).

The table's hub passes on at most :data:`GROUP_SEND_BUDGET` envelopes per
:data:`GROUP_SEND_BUDGET_SECONDS` — one per message per household it goes
to. A sender's household applies the same budget to what its own people
send to one table (so they hear ``429`` instead of the hub dropping it).
A sliding window: each message *costs* the envelopes it will take.
"""

from __future__ import annotations

import math
from collections import deque

from ..domain.app_group import GROUP_SEND_BUDGET, GROUP_SEND_BUDGET_SECONDS


class TableSendBudget:
    """Envelopes spent at one table in the last window."""

    __slots__ = ("_limit", "_spent", "_total", "_window")

    def __init__(
        self,
        limit: int = GROUP_SEND_BUDGET,
        window: float = GROUP_SEND_BUDGET_SECONDS,
    ) -> None:
        self._limit = limit
        self._window = window
        #: (when, cost), oldest first.
        self._spent: deque[tuple[float, int]] = deque()
        self._total = 0

    def _expire(self, now: float) -> None:
        while self._spent and self._spent[0][0] <= now - self._window:
            _when, cost = self._spent.popleft()
            self._total -= cost

    def take(self, cost: int, now: float) -> int | None:
        """Spend *cost* envelopes at *now* (monotonic seconds) — ``None``
        when they fit, else the whole seconds until they would (nothing
        spent). A message costing nothing (only this household's people)
        always fits."""
        if cost <= 0:
            return None
        self._expire(now)
        if self._total + cost <= self._limit:
            self._spent.append((now, cost))
            self._total += cost
            return None
        if cost > self._limit:
            return math.ceil(self._window)
        # Wait until enough of the oldest spending runs out of the window.
        need = self._total + cost - self._limit
        freed = 0
        for when, spent in self._spent:
            freed += spent
            if freed >= need:
                return max(1, math.ceil(when + self._window - now))
        return math.ceil(self._window)  # pragma: no cover — freed covers need

    def idle(self, now: float) -> bool:
        """Nothing spent in the window (the budget can be forgotten)."""
        self._expire(now)
        return not self._spent
