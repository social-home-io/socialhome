"""Bounded, in-memory retry queue for ``POST /gfs/publish``.

:meth:`GfsConnectionService.publish_space_event` relays a public/global
space event to every connection server the space is published on. Before
this queue a failed POST — the GFS restarting, a network blip, a 5xx, the
GFS's per-IP limiter answering 429 — lost the event for every subscriber
on that server. Now a *transient* failure (transport error, timeout, 408,
429, 5xx) is queued per connection and re-POSTed with backoff, honouring a
429's ``Retry-After``; a *permanent* one (any other 4xx, an unfollowed
redirect) is not retried, because the identical body gets the identical
answer.

**What is queued is only ``{space_id, event_type, payload}``** — the
identity-free relay body (:class:`GfsPublish`). There is no slot for
``from_instance`` or a household signature, so a retry cannot add identity
by construction; the sender re-checks ``anonymous_publish`` before every
retry and drops the item rather than fall back to the identified body.

**Why in memory, not the durable outbox or a new table.** The outbox
(``federation_outbox``) is keyed by a recipient *instance* and stores a
signed, per-peer encrypted envelope that is re-signed on redelivery — a
GFS publish has no recipient instance and must carry no household
signature, so it would need a new row kind (a migration) and a redelivery
branch that must never sign. A dedicated table is a migration too. Neither
is warranted: the payload is already the authority-signed public
ciphertext, subscribers dedupe by post id, and the outage a retry bridges
is minutes long (the budget below is ~13 minutes). A restart losing the
queue costs what it cost before this queue existed, and the owner can
always republish. Bounded by :data:`GFS_PUBLISH_RETRY_MAX_PENDING`.

Order is kept per connection: while a connection has pending items, a new
publish to it joins the back of its queue instead of overtaking.

Lifecycle follows the scheduler pattern (``_stop: asyncio.Event``, see
``infrastructure/replay_cache_scheduler.py``); a second event wakes the
loop when work is queued.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

log = logging.getLogger(__name__)

#: Wait before retry *n + 1* of a connection's head item; the length is the
#: retry budget. ~5 s, 30 s, 2 min, 10 min — about 13 minutes in all, which
#: rides out a GFS restart or a network blip without parking work for
#: hours. A ``Retry-After`` longer than the floor wins.
GFS_PUBLISH_RETRY_BACKOFF_S: tuple[float, ...] = (5.0, 30.0, 120.0, 600.0)

#: Ceiling on a ``Retry-After`` we honour. The GFS sends 60 s; a broken or
#: hostile server must not park a retry for a day.
GFS_PUBLISH_MAX_RETRY_AFTER_S: float = 900.0

#: Ceiling on queued publishes across every connection. Past it a failed
#: publish is lost as before (logged once until the queue drains).
GFS_PUBLISH_RETRY_MAX_PENDING: int = 256


@dataclass(slots=True, frozen=True)
class GfsPublish:
    """One identity-free relay body waiting for a retry. Process-local queue
    item, never persisted. Exactly the ``POST /gfs/publish`` body."""

    space_id: str
    event_type: str
    payload: dict

    def body(self) -> dict:
        """The wire body — ``{space_id, event_type, payload}``, nothing else."""
        return {
            "space_id": self.space_id,
            "event_type": self.event_type,
            "payload": self.payload,
        }


@dataclass(slots=True, frozen=True)
class PublishOutcome:
    """How one ``POST /gfs/publish`` attempt ended."""

    kind: Literal["delivered", "transient", "permanent"]
    #: Seconds the server asked us to wait (a 429's ``Retry-After``).
    retry_after_s: float | None = None

    @classmethod
    def delivered(cls) -> PublishOutcome:
        return cls("delivered")

    @classmethod
    def transient(cls, retry_after_s: float | None = None) -> PublishOutcome:
        return cls("transient", retry_after_s)

    @classmethod
    def permanent(cls) -> PublishOutcome:
        return cls("permanent")


def parse_retry_after_s(value: str | None) -> float | None:
    """``Retry-After`` as delta-seconds (capped), or ``None`` when unusable.

    Only the delta-seconds form is honoured — the GFS sends it. The
    HTTP-date form is ignored rather than trusted against a remote clock.
    """
    if not value:
        return None
    try:
        seconds = int(value.strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return min(float(seconds), GFS_PUBLISH_MAX_RETRY_AFTER_S)


def classify_publish_status(status: int, retry_after: str | None) -> PublishOutcome:
    """Map a ``POST /gfs/publish`` status to an outcome.

    2xx delivered; 408 / 429 / 5xx transient (429 carries ``Retry-After``);
    everything else permanent — a 4xx is the same answer on every attempt,
    and a 3xx is never followed (the POST runs with ``allow_redirects=False``).
    """
    if 200 <= status < 300:
        return PublishOutcome.delivered()
    if status in (408, 429) or status >= 500:
        return PublishOutcome.transient(parse_retry_after_s(retry_after))
    return PublishOutcome.permanent()


@dataclass(slots=True)
class _ConnQueue:
    """Pending publishes for one connection. Process-local."""

    items: deque[GfsPublish] = field(default_factory=deque)
    #: ``time.monotonic()`` when the head item is next tried.
    due_at: float = 0.0
    #: Retries of the head item that failed in a row (reset on progress).
    retries_done: int = 0


#: ``send(conn_id, item)`` → outcome. Supplied by the owner (the GFS
#: connection service), which re-checks the connection and the publication
#: before every attempt.
PublishSender = Callable[[str, GfsPublish], Awaitable[PublishOutcome]]


class GfsPublishRetryQueue:
    """Per-connection FIFO of failed GFS publishes, retried with backoff."""

    __slots__ = ("_send", "_queues", "_task", "_stop", "_wake", "_full_logged")

    def __init__(self, send: PublishSender) -> None:
        self._send = send
        self._queues: dict[str, _ConnQueue] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._full_logged = False

    def pending(self, conn_id: str) -> bool:
        """Whether ``conn_id`` has publishes waiting (new ones queue behind)."""
        return conn_id in self._queues

    def enqueue(
        self,
        conn_id: str,
        item: GfsPublish,
        *,
        retry_after_s: float | None = None,
    ) -> bool:
        """Queue ``item`` for ``conn_id``. ``False`` when it was refused
        (stopped, or the queue is full) — the caller's miss is then final.

        A connection's first item is due after the first backoff step (or
        ``retry_after_s`` when longer); a later item joins behind it.
        """
        if self._stop.is_set():
            return False
        if sum(len(q.items) for q in self._queues.values()) >= (
            GFS_PUBLISH_RETRY_MAX_PENDING
        ):
            if not self._full_logged:
                self._full_logged = True
                log.warning(
                    "GFS publish retry queue is full (%d) — further failed"
                    " publishes are lost until it drains",
                    GFS_PUBLISH_RETRY_MAX_PENDING,
                )
            return False
        queued = GfsPublish(
            space_id=item.space_id,
            event_type=item.event_type,
            payload=copy.deepcopy(item.payload),
        )
        conn = self._queues.get(conn_id)
        if conn is None:
            conn = _ConnQueue(due_at=self._due(0, retry_after_s))
            self._queues[conn_id] = conn
        conn.items.append(queued)
        self._wake.set()
        return True

    async def start(self) -> None:
        """Start the retry loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="gfs-publish-retry")

    async def stop(self) -> None:
        """Stop the loop, wait for it, and drop what is still pending.

        Idempotent. The queue is in memory by design (see the module
        docstring), so pending publishes are logged as lost.
        """
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None
        pending = sum(len(q.items) for q in self._queues.values())
        if pending:
            log.warning(
                "GFS publish retry: dropping %d pending GFS publish(es) on shutdown",
                pending,
            )
        self._queues.clear()
        self._full_logged = False

    @staticmethod
    def _due(retries_done: int, retry_after_s: float | None) -> float:
        floor = GFS_PUBLISH_RETRY_BACKOFF_S[
            min(retries_done, len(GFS_PUBLISH_RETRY_BACKOFF_S) - 1)
        ]
        return time.monotonic() + max(floor, retry_after_s or 0.0)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                await self._run_due()
            except Exception as exc:  # pragma: no cover — defensive
                log.warning("GFS publish retry pass failed: %s", exc)
            if self._stop.is_set():
                return
            timeout = None
            if self._queues:
                next_due = min(q.due_at for q in self._queues.values())
                timeout = max(0.0, next_due - time.monotonic())
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except TimeoutError:
                continue

    async def _run_due(self) -> None:
        """Retry every connection whose head item is due, in order."""
        for conn_id in list(self._queues):
            conn = self._queues.get(conn_id)
            if conn is None or conn.due_at > time.monotonic():
                continue
            await self._drain_conn(conn_id, conn)
            if self._stop.is_set():
                return

    async def _drain_conn(self, conn_id: str, conn: _ConnQueue) -> None:
        while conn.items and not self._stop.is_set():
            item = conn.items[0]
            try:
                outcome = await self._send(conn_id, item)
            except Exception:
                log.warning(
                    "GFS publish retry to %s raised; treating it as transient",
                    conn_id,
                    exc_info=True,
                )
                outcome = PublishOutcome.transient()
            if outcome.kind == "delivered":
                conn.items.popleft()
                conn.retries_done = 0
                continue
            if outcome.kind == "permanent":
                conn.items.popleft()
                log.warning(
                    "GFS publish retry: %s refused %s@%s for good — not retrying it",
                    conn_id,
                    item.event_type,
                    item.space_id,
                )
                continue
            conn.retries_done += 1
            if conn.retries_done >= len(GFS_PUBLISH_RETRY_BACKOFF_S):
                log.warning(
                    "GFS publish retry: giving up on %d GFS publish(es) to %s"
                    " after %d retries: %s",
                    len(conn.items),
                    conn_id,
                    conn.retries_done,
                    ", ".join(f"{q.event_type}@{q.space_id}" for q in conn.items),
                )
                conn.items.clear()
                break
            conn.due_at = self._due(conn.retries_done, outcome.retry_after_s)
            return
        if not conn.items and self._queues.get(conn_id) is conn:
            del self._queues[conn_id]
            if not self._queues:
                self._full_logged = False
