"""OrderedPublisher — publish domain events per key in the order they were
decided, without making the next writer wait for the previous publish.

The :class:`~socialhome.infrastructure.event_bus.EventBus` runs every
handler inline, and some handlers are slow: federation outbound tries a
direct send to each household (≈ 10 s before a dead one falls back to the
outbox). A service that publishes under its own lock (to keep "stored" and
"announced" in step) makes every later write by the same actor wait for
those sends.

Instead the service decides the order under its lock and hands the event
over with :meth:`OrderedPublisher.enqueue`; the events of one key are
published one at a time, oldest first, by a task of their own:

* **Order holds.** An event is never published before an event queued
  earlier under the same key, so a frame can't overtake an audience
  change or a removal (or the other way round).
* **Nobody waits on someone else's publish.** When the key was idle,
  ``enqueue`` returns a waiter the caller may await after leaving its lock
  — a lone write stays synchronous, as with a plain ``bus.publish``. When
  an earlier event is still being published, it returns ``None``: the
  event goes out after that one, and the caller returns now.
* **A cancelled request loses nothing.** Publishing runs in its own task,
  so a client hanging up doesn't drop events already decided.

``flush`` waits until everything queued is published (tests, shutdown);
``stop`` does the same with a time limit and then cancels.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..domain.events import DomainEvent
    from ..infrastructure.event_bus import EventBus

log = logging.getLogger(__name__)


class OrderedPublisher:
    """Per-key FIFO publication onto an :class:`EventBus`."""

    __slots__ = ("_bus", "_drainers", "_queues")

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._queues: dict[
            str, deque[tuple[DomainEvent, asyncio.Future[None] | None]]
        ] = {}
        self._drainers: dict[str, asyncio.Task[None]] = {}

    def enqueue(self, key: str, event: DomainEvent) -> asyncio.Future[None] | None:
        """Queue *event* behind every event already queued under *key*.

        Call it where the order is decided (under the caller's lock). Returns
        a future that resolves once *event* is published when *key* was
        idle, else ``None`` (see the module docstring). Await the future
        through :func:`asyncio.shield` after releasing the lock.
        """
        idle = key not in self._drainers
        waiter: asyncio.Future[None] | None = (
            asyncio.get_running_loop().create_future() if idle else None
        )
        self._queues.setdefault(key, deque()).append((event, waiter))
        if idle:
            self._drainers[key] = asyncio.create_task(
                self._drain(key), name=f"ordered-publish:{key}"
            )
        return waiter

    async def publish(self, key: str, event: DomainEvent) -> None:
        """:meth:`enqueue` and wait when *key* was idle — for a caller
        without a lock of its own."""
        waiter = self.enqueue(key, event)
        if waiter is not None:
            await asyncio.shield(waiter)

    def pending(self, key: str) -> int:
        """Events queued under *key* and not yet taken for publishing."""
        return len(self._queues.get(key, ()))

    async def flush(self) -> None:
        """Wait until every queued event is published."""
        while self._drainers:
            await asyncio.gather(*self._drainers.values(), return_exceptions=True)

    async def stop(self, timeout: float) -> None:
        """:meth:`flush` for at most *timeout* seconds, then cancel the rest
        (shutdown: an unpublished event is logged, never silently lost)."""
        try:
            await asyncio.wait_for(self.flush(), timeout)
        except TimeoutError:
            for task in list(self._drainers.values()):
                task.cancel()
            await asyncio.gather(*self._drainers.values(), return_exceptions=True)

    async def _drain(self, key: str) -> None:
        queue = self._queues[key]
        try:
            while queue:
                event, waiter = queue.popleft()
                try:
                    await self._bus.publish(event)
                finally:
                    if waiter is not None and not waiter.done():
                        waiter.set_result(None)
        finally:
            # No await between the last ``while queue`` check and here, so
            # nothing can be queued under this key without a drainer.
            self._drainers.pop(key, None)
            if queue:
                log.warning(
                    "ordered publish %s stopped with %d event(s) unpublished",
                    key,
                    len(queue),
                )
                for _event, waiter in queue:
                    if waiter is not None and not waiter.done():
                        waiter.set_result(None)
                queue.clear()
            self._queues.pop(key, None)
