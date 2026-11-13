"""One sender's inbound events are applied in arrival order, and a handler
that failed for a passing reason is run again in place.

An inbound envelope is verified, its ``msg_id`` is stored in the replay
cache, and only then are its handlers run (§24.11). A handler that fails
after that — the database was locked, a file could not be written — used to
lose the event: the sender was told (or assumed) the delivery worked, and a
re-send of the same envelope is refused as a replay.

:class:`InboundRetry` sits behind
:meth:`~socialhome.federation.event_dispatch_registry.EventDispatchRegistry.dispatch`,
which every transport ends in, and does two things:

* **A lane per sender.** Every event takes its sender's lane (an
  ``asyncio.Lock`` keyed by ``from_instance`` — for a mesh-routed inner
  event that is the origin household, not the relay) before its handlers
  run and keeps it until they succeeded, refused or were given up. So one
  household's events are applied strictly in the order they arrived here —
  always, not only after a failure: a delete never overtakes the create it
  follows, a demotion never lands before the promotion. Later events simply
  wait on the lock; nothing is queued aside and nothing is dropped. The
  wait is bounded (:data:`LANE_WAIT_S`): an event whose turn has not come
  by then runs anyway — see below.
* **The retry, in place.** A handler that raised one of
  :data:`TRANSIENT_ERRORS` is run again — only it — after
  :data:`RETRY_DELAYS_S`, while the lane is held; then one WARNING. It is
  this household's own second try of an envelope it already verified:
  nothing is asked of the sender and the replay cache is not touched.
  Before each later try the gate (still a peer, the post-decrypt gates) is
  asked again, so an unpair or a ban that landed meanwhile wins.

:data:`NEVER_RETRIED` events take no lane and get no second try. A dispatch
made from inside a handler (``replay_held``: a parent landed, a seat went
live) runs in place, without a second lane — the task already holds one, and
taking another could deadlock two senders against each other.

**Why the wait for a lane is bounded.** Handlers answer their sender in
place, and over the HTTPS inbox a send waits for the receiver's answer,
which comes only after ITS dispatch. Two households that handle each
other's events at the same moment would each hold the lane the other's
answer needs — neither could finish until a POST timed out. So a turn that
has not come within :data:`LANE_WAIT_S` is taken anyway (one WARNING): the
cycle unwinds before the sender's POST gives up, and order is given up only
for a turn that took longer than every retry does.

In memory on purpose: an event being retried when the process dies is lost,
as before this existed. Lanes are ordered per sender only: two households'
events about the same thing (a host relaying a delete, the author's own
create) are not ordered against each other.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Sequence

from ..domain.federation import FederationEvent, FederationEventType
from ..domain.federation_retention import SHORT_LIVED

log = logging.getLogger(__name__)

#: Seconds before the 2nd and 3rd try — a lock is over in seconds.
RETRY_DELAYS_S: tuple[float, ...] = (1.0, 2.0)

#: No try starts later than this after the event's first run began (a
#: "database is locked" takes the 5 s busy timeout to surface, so the
#: delays alone do not bound the hold).
RETRY_WINDOW_S: float = 4.0

#: How long an event waits for its sender's lane before it runs anyway.
#: Above :data:`RETRY_WINDOW_S`, so a retry is never overtaken; below what
#: waits on the other side while a lane is held — the sender's inbox POST
#: (10 s total, ``HttpsInboxTransport``), and the read loops of a peer's
#: DataChannel, the GFS relay socket and a mailbox claim (30 s heartbeat,
#: 15 s pong window) — with room left for the handlers to answer.
LANE_WAIT_S: float = 6.0

#: How long :meth:`InboundRetry.stop` waits for the last tries it set off.
STOP_WAIT_S: float = 5.0

#: The only failures tried again — the ones that pass on their own: the
#: database was locked or busy, an I/O call failed or timed out
#: (``TimeoutError`` is an ``OSError``). Everything else — a refusal
#: (``ValueError``), what a malformed payload raises (``KeyError``,
#: ``TypeError``, ``AttributeError``), a row already stored
#: (``IntegrityError``), a ``RuntimeError`` ("not configured", a domain
#: refusal), a bug — fails the same way again, and a second run would only
#: repeat whatever the handler did before it raised.
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    sqlite3.OperationalError,
    OSError,
)


def is_transient(exc: BaseException) -> bool:
    """Whether a handler that raised ``exc`` is worth running again."""
    return isinstance(exc, TRANSIENT_ERRORS)


#: Events that take no lane and get no second try: a call that has stopped
#: ringing (:data:`~socialhome.domain.federation_retention.SHORT_LIVED`), a
#: typing indicator, transport signalling (none may wait behind content),
#: the sync streams (stateful, with their own resume; they travel beside
#: the live events, unordered with them), and the ``SPACE_ROUTED`` wrapper —
#: it comes from the relay, and the inner event it unwraps is dispatched on
#: its own, in its origin's lane.
NEVER_RETRIED: frozenset[FederationEventType] = (
    SHORT_LIVED
    | frozenset(
        {
            FederationEventType.SPACE_ROUTED,
            FederationEventType.DM_USER_TYPING,
            FederationEventType.FEDERATION_RTC_OFFER,
            FederationEventType.FEDERATION_RTC_ANSWER,
            FederationEventType.FEDERATION_RTC_ICE,
            FederationEventType.DM_HISTORY_CHUNK,
            FederationEventType.DM_HISTORY_CHUNK_ACK,
        }
    )
    | frozenset(t for t in FederationEventType if t.value.startswith("space_sync_"))
)

EventHandler = Callable[[FederationEvent], Awaitable[None]]
#: A handler and what it raised.
Failed = tuple[EventHandler, BaseException]
#: Runs ``handlers`` (``None``: every handler of the event's type) on an
#: event and returns the ones that raised — the registry's own loop.
Runner = Callable[
    [FederationEvent, Sequence[EventHandler] | None], Awaitable[list[Failed]]
]
#: Whether an event may still be applied on a later try.
RetryGate = Callable[[FederationEvent], Awaitable[bool]]


def log_handler_failure(
    event: FederationEvent, handler: EventHandler, exc: BaseException
) -> None:
    """A handler failure that is final — the event is lost for that handler."""
    log.error(
        "Event handler %s failed for %s",
        getattr(handler, "__qualname__", repr(handler)),
        event.event_type,
        exc_info=exc,
    )


class _Lane:
    """One sender's turn: the lock and how many events hold or wait on it
    (the entry goes when none do — the shape of ``_SeatTurn``)."""

    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


class InboundRetry:
    """Per-sender lanes with the in-place second tries."""

    __slots__ = (
        "_delays",
        "_gate",
        "_idle",
        "_in_lane",
        "_lane_wait",
        "_lanes",
        "_retrying",
        "_stop",
        "_window",
    )

    def __init__(
        self,
        *,
        gate: RetryGate | None = None,
        delays: tuple[float, ...] = RETRY_DELAYS_S,
        window: float = RETRY_WINDOW_S,
        lane_wait: float = LANE_WAIT_S,
    ) -> None:
        self._gate = gate
        self._delays = delays
        self._window = window
        self._lane_wait = lane_wait
        self._lanes: dict[str, _Lane] = {}
        #: Tasks running handlers inside a lane (see the module docstring).
        self._in_lane: set[asyncio.Task[object]] = set()
        self._retrying = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._stop = asyncio.Event()

    def __len__(self) -> int:
        """Lanes in use — senders with an event running or waiting."""
        return len(self._lanes)

    async def dispatch(self, event: FederationEvent, run: Runner) -> None:
        """Run ``event``'s handlers (``run``) in its sender's turn; run the
        ones that failed for a passing reason again before the turn ends."""
        task = asyncio.current_task()
        if task is None or task in self._in_lane:
            await self._apply(event, run)
            return
        key = event.from_instance
        lane = self._lanes.get(key)
        if lane is None:
            lane = self._lanes[key] = _Lane()
        lane.users += 1
        in_turn = False
        try:
            try:
                await asyncio.wait_for(lane.lock.acquire(), timeout=self._lane_wait)
                in_turn = True
            except TimeoutError:
                # Never dropped, never left waiting: see the module docstring.
                log.warning(
                    "%s from %s: its turn did not come within %ss — applied"
                    " out of turn",
                    event.event_type,
                    key,
                    self._lane_wait,
                )
            self._in_lane.add(task)
            try:
                await self._apply(event, run)
            finally:
                self._in_lane.discard(task)
        finally:
            if in_turn:
                lane.lock.release()
            lane.users -= 1
            if lane.users == 0:
                del self._lanes[key]

    async def stop(self) -> None:
        """End every wait between tries: each event being retried gets one
        last try now (a handler is never cut mid-write) and its lane is
        released. Idempotent."""
        self._stop.set()
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=STOP_WAIT_S)
        except TimeoutError:
            log.warning(
                "%d inbound event(s) still in their last try at shutdown",
                self._retrying,
            )

    async def _apply(self, event: FederationEvent, run: Runner) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        failed = self._transient(event, await run(event, None))
        if not failed:
            return
        tries = 1
        self._retrying += 1
        self._idle.clear()
        try:
            for delay in self._delays:
                if loop.time() - started + delay > self._window:
                    break
                stopping = await self._stopping_after(delay)
                if not await self._allowed(event):
                    return
                tries += 1
                failed = self._transient(
                    event, await run(event, [h for h, _ in failed])
                )
                if not failed:
                    log.info(
                        "%s from %s: applied on try %d",
                        event.event_type,
                        event.from_instance,
                        tries,
                    )
                    return
                if stopping:
                    break
            log.warning(
                "%s from %s: %d handler(s) still failing after %d tries — the"
                " event is dropped",
                event.event_type,
                event.from_instance,
                len(failed),
                tries,
                exc_info=failed[-1][1],
            )
        finally:
            self._retrying -= 1
            if not self._retrying:
                self._idle.set()

    def _transient(self, event: FederationEvent, failed: list[Failed]) -> list[Failed]:
        """The failures worth another try; every other one is final."""
        again: list[Failed] = []
        for handler, exc in failed:
            if is_transient(exc):
                again.append((handler, exc))
            else:
                log_handler_failure(event, handler, exc)
        return again

    async def _stopping_after(self, delay: float) -> bool:
        """Wait ``delay`` seconds; ``True`` when :meth:`stop` cut it short."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except TimeoutError:
            return False
        return True

    async def _allowed(self, event: FederationEvent) -> bool:
        if self._gate is None:
            return True
        try:
            allowed = await self._gate(event)
        except Exception:
            log.warning(
                "%s from %s: the retry gate failed — the event is dropped",
                event.event_type,
                event.from_instance,
                exc_info=True,
            )
            return False
        if not allowed:
            log.info(
                "%s from %s: no longer allowed — not tried again",
                event.event_type,
                event.from_instance,
            )
        return allowed
