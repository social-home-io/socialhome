"""Notification delivery off the publisher's path.

The :class:`~socialhome.infrastructure.event_bus.EventBus` awaits every
handler inline, inside whatever request or federation-inbound task raised
the event. A space post to 200 members used to resolve, write and push all
200 notifications before the poster's request returned — and a federation
inbound waited the same way.

:class:`NotificationDeliveryWorker` takes that work off the publisher's
path. :meth:`NotificationService.wire` subscribes each handler through
:meth:`NotificationDeliveryWorker.deferred`, so the publisher only puts
**one job per triggering event** (the handler and the event — never one
per recipient) on a bounded :class:`asyncio.Queue` and returns. One
background task drains the queue in order and runs each job, which
resolves the audience and writes the rows as one batch
(:meth:`NotificationService.run_batched`), then publishes the
``NotificationCreated`` frames and sends the pushes (off the event loop,
:class:`~socialhome.services.push_service.PushService`).

* **Order.** One consumer, first in first out: a recipient sees a thing's
  notifications in the order the events were published, and a later
  delete clears rows an earlier job wrote.
* **Backpressure, never drops.** A full queue makes the publisher wait
  for a free slot (``await put``) — the burst slows down, nothing is
  lost. A wait longer than ``full_warning_seconds`` logs a WARNING.
* **Lifecycle.** The ``asyncio.Event`` pattern of
  :mod:`~socialhome.infrastructure.replay_cache_scheduler`. :meth:`stop`
  lets the job in hand finish, then delivers what is still queued within
  ``drain_timeout`` seconds (a WARNING names what is left). Before
  :meth:`start` and after :meth:`stop` a job runs inline in the
  publisher, as the bus would — nothing is dropped at the edges.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from ..domain.events import DomainEvent
from ..infrastructure.event_bus import E, Handler

log = logging.getLogger(__name__)

#: Jobs waiting at most. A job is one event (a post, a comment), not one
#: recipient, so this is a large burst of activity.
DEFAULT_MAX_QUEUED: int = 1000

#: How long :meth:`NotificationDeliveryWorker.stop` keeps delivering what
#: is still queued before it gives up on the rest.
DEFAULT_DRAIN_TIMEOUT: float = 10.0

#: A publisher that waits this long for a free slot logs a WARNING.
DEFAULT_FULL_WARNING_SECONDS: float = 5.0


@dataclass(slots=True, frozen=True)
class _Job:
    """Service-local: one triggering event and the handler that delivers
    its notifications."""

    handler: Handler[Any]
    event: DomainEvent


class NotificationDeliveryWorker:
    """Runs notification handlers in one background task, in order."""

    __slots__ = (
        "_drain_timeout",
        "_full_warning",
        "_queue",
        "_stop",
        "_task",
    )

    def __init__(
        self,
        *,
        max_queued: int = DEFAULT_MAX_QUEUED,
        drain_timeout: float = DEFAULT_DRAIN_TIMEOUT,
        full_warning_seconds: float = DEFAULT_FULL_WARNING_SECONDS,
    ) -> None:
        self._queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=max_queued)
        self._drain_timeout = drain_timeout
        self._full_warning = full_warning_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        """Whether jobs go to the background task (else they run inline)."""
        return self._task is not None and not self._stop.is_set()

    @property
    def queued(self) -> int:
        """Jobs waiting (not counting the one in hand)."""
        return self._queue.qsize()

    def deferred(self, handler: Handler[E]) -> Handler[E]:
        """*handler* as a bus subscriber that hands each event to this
        worker instead of running it in the publisher."""

        async def enqueue(event: E) -> None:
            await self.submit(handler, event)

        enqueue.__qualname__ = getattr(handler, "__qualname__", repr(handler))
        return enqueue

    async def submit(self, handler: Handler[E], event: E) -> None:
        """Queue one job; wait for a free slot when the queue is full.

        While the worker is not running the job runs right here — and so
        does an event a job itself publishes, which must never wait on the
        queue its own task drains."""
        job = _Job(handler, event)
        if not self.running or asyncio.current_task() is self._task:
            await self._run(job)
            return
        try:
            self._queue.put_nowait(job)
            return
        except asyncio.QueueFull:
            pass
        try:
            await asyncio.wait_for(self._queue.put(job), timeout=self._full_warning)
            return
        except TimeoutError:
            log.warning(
                "notification delivery: queue full (%d jobs) for %.0f s — "
                "%s waits for a free slot",
                self._queue.maxsize,
                self._full_warning,
                type(event).__name__,
            )
        await self._queue.put(job)

    async def drain(self) -> None:
        """Wait until every job queued so far is delivered (tests, and
        anything that must read the rows an event wrote)."""
        if self._task is not None:
            await self._queue.join()

    async def start(self) -> None:
        """Start the background task. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Finish the job in hand, deliver what is queued (bounded by
        ``drain_timeout``), then stop. Later jobs run inline."""
        self._stop.set()
        task, self._task = self._task, None
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout=self._drain_timeout)
        except TimeoutError, asyncio.CancelledError:
            task.cancel()
        try:
            await asyncio.wait_for(self._deliver_rest(), timeout=self._drain_timeout)
        except TimeoutError:
            log.warning(
                "notification delivery: stopped after %.0f s with jobs "
                "undelivered (%d more queued)",
                self._drain_timeout,
                self._queue.qsize(),
            )

    async def _loop(self) -> None:
        stopping = asyncio.create_task(self._stop.wait())
        try:
            while not self._stop.is_set():
                getting = asyncio.create_task(self._queue.get())
                await asyncio.wait(
                    (getting, stopping), return_when=asyncio.FIRST_COMPLETED
                )
                if not getting.done():
                    getting.cancel()
                    break
                try:
                    await self._run(getting.result())
                finally:
                    self._queue.task_done()
        finally:
            stopping.cancel()

    async def _deliver_rest(self) -> None:
        while not self._queue.empty():
            job = self._queue.get_nowait()
            try:
                await self._run(job)
            finally:
                self._queue.task_done()

    @staticmethod
    async def _run(job: _Job) -> None:
        """One job; a failure is logged and never stops the next one (the
        bus's own rule)."""
        try:
            await job.handler(job.event)
        except Exception:
            log.exception(
                "notification delivery: %s failed for %s",
                getattr(job.handler, "__qualname__", repr(job.handler)),
                type(job.event).__name__,
            )
