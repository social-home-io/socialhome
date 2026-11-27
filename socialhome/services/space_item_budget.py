"""Sender budgets for a GFS space's channel items (v_97).

Per household and space, in priority order authority > content > chat >
location (``docs/protocol/space-items.md`` › "Sender budgets and
location"):

* **chat** — at most :data:`CHAT_PER_MINUTE` items a minute; the rest wait
  for the next minute, in order;
* **location** — at most one pin per person per :data:`LOCATION_INTERVAL_S`,
  latest only: a newer pin replaces one still waiting;
* **content and authority** — never held here (the GFS's own publish
  budgets and the person's own pace bound them).

A held item is published by a background task when its slot opens; it is
never dropped, except a location pin a newer one replaced. In memory only:
a restart forgets what waited (the next pin, the next message carry on).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable

from ..domain.space_item import BUDGET_CHAT, BUDGET_LOCATION

log = logging.getLogger(__name__)

CHAT_PER_MINUTE: int = 20
LOCATION_INTERVAL_S: float = 60.0
#: Items one space may hold waiting (beyond it a chat item goes at once —
#: never dropped).
MAX_WAITING: int = 200

Publish = Callable[[], Awaitable[None]]


class SenderBudget:
    """Pace chat and location items per space (see the module docstring)."""

    __slots__ = ("_chat", "_clock", "_pins", "_queues", "_tasks")

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        #: space id → send times of the last minute's chat items.
        self._chat: dict[str, deque[float]] = {}
        #: space id → chat items waiting, in order.
        self._queues: dict[str, deque[Publish]] = {}
        #: (space id, person) → (last sent, the newest pin waiting).
        self._pins: dict[tuple[str, str], tuple[float, Publish | None]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def submit(
        self, space_id: str, budget: str, person: str, publish: Publish
    ) -> bool:
        """Publish now (``True``) or hold it for its slot (``False``)."""
        if budget == BUDGET_LOCATION:
            return await self._pin(space_id, person, publish)
        if budget == BUDGET_CHAT:
            return await self._chat_item(space_id, publish)
        await publish()
        return True

    async def _pin(self, space_id: str, person: str, publish: Publish) -> bool:
        key = (space_id, person)
        now = self._clock()
        last, waiting = self._pins.get(key, (-LOCATION_INTERVAL_S, None))
        if now - last >= LOCATION_INTERVAL_S and waiting is None:
            self._pins[key] = (now, None)
            await publish()
            return True
        self._pins[key] = (last, publish)
        if waiting is None:
            self._spawn(self._pin_later(key, last + LOCATION_INTERVAL_S - now))
        return False

    async def _pin_later(self, key: tuple[str, str], delay: float) -> None:
        await asyncio.sleep(max(0.0, delay))
        _last, waiting = self._pins.get(key, (0.0, None))
        if waiting is None:
            return
        self._pins[key] = (self._clock(), None)
        try:
            await waiting()
        except Exception:
            log.warning("space item budget: a held pin failed", exc_info=True)

    async def _chat_item(self, space_id: str, publish: Publish) -> bool:
        now = self._clock()
        sent = self._chat.setdefault(space_id, deque())
        while sent and now - sent[0] >= 60.0:
            sent.popleft()
        queue = self._queues.setdefault(space_id, deque())
        if (len(sent) < CHAT_PER_MINUTE and not queue) or len(queue) >= MAX_WAITING:
            sent.append(now)
            await publish()
            return True
        queue.append(publish)
        if len(queue) == 1:
            self._spawn(self._drain(space_id))
        return False

    async def _drain(self, space_id: str) -> None:
        queue = self._queues.get(space_id)
        sent = self._chat.setdefault(space_id, deque())
        while queue:
            now = self._clock()
            while sent and now - sent[0] >= 60.0:
                sent.popleft()
            if len(sent) >= CHAT_PER_MINUTE:
                await asyncio.sleep(max(0.05, 60.0 - (now - sent[0])))
                continue
            publish = queue.popleft()
            sent.append(self._clock())
            try:
                await publish()
            except Exception:
                log.warning("space item budget: a held item failed", exc_info=True)

    async def wait_idle(self) -> None:
        """Wait for every held item (tests, graceful stop)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
