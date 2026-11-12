"""Second tries for an inbound event whose handler failed for a passing reason.

An inbound envelope is verified, its ``msg_id`` is stored in the replay
cache, and only then are its handlers run (§24.11). A handler that fails
after that — the database was locked, a file could not be written — used to
lose the event: the sender was told (or assumed) the delivery worked, and a
re-send of the same envelope is refused as a replay. What the space sync
carries came back at the next sync; a direct message, a reaction, a pairing
or key event did not.

:class:`InboundRetry` keeps such an event in memory and runs the handlers
that failed — only those — again after :data:`RETRY_DELAYS_S`. It is this
household's own second try of an envelope it has already verified; nothing
is asked of the sender and the replay cache is not touched, so a replayed
envelope is refused exactly as before. It sits behind
:meth:`~socialhome.federation.event_dispatch_registry.EventDispatchRegistry.dispatch`,
which every transport ends in (HTTPS inbox, DataChannel, GFS relay, GFS
mailbox and multi-recipient frames, mesh ``SPACE_ROUTED`` inner events).

**One sender's events stay in order.** While a household (``from_instance``
— for a mesh-routed event the origin, not the relay) has a held event,
everything else it sends waits behind it in one queue and is dispatched in
arrival order once the head succeeded or was given up: a delete never
overtakes the create it follows, a demotion never lands before the
promotion. Other senders are not held up.

What is never tried again, and never waits:

* a failure that would repeat — anything but :data:`TRANSIENT_ERRORS`: a
  refusal, a malformed payload, a programming error;
* :data:`NEVER_RETRIED` events — call negotiation, typing, transport
  signalling and the sync streams, which are stale within seconds or have
  their own resume.

Before every delayed run the gate (the post-decrypt gates and "still a
peer") is asked again, so a ban or an unpair that landed meanwhile wins.

Bounded (events, events per sender, bytes). An event that does not fit
behind a sender's head ends the wait: the head is given up at once (one
WARNING) and the queue runs now, in order — never out of order, never
dropped silently. In memory on purpose: a restart loses what was held, the
outcome before this existed. :meth:`InboundRetry.stop` lets a handler that
is running finish and drops the rest with one log line.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

import orjson

from ..domain.federation import FederationEvent, FederationEventType
from ..domain.federation_retention import SHORT_LIVED

log = logging.getLogger(__name__)

#: Seconds before the 2nd, 3rd and 4th try — a lock is over in seconds.
RETRY_DELAYS_S: tuple[float, ...] = (1.0, 5.0, 20.0)

DEFAULT_MAX_ENTRIES = 256
DEFAULT_MAX_PER_SENDER = 64
DEFAULT_MAX_BYTES = 4 * 1024 * 1024

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


#: Events a late second try would only hurt, so they are neither held nor
#: made to wait: a call that has stopped ringing
#: (:data:`~socialhome.domain.federation_retention.SHORT_LIVED`), a typing
#: indicator, transport signalling, the sync streams (stateful, with their
#: own resume; they already travel beside the live events, unordered with
#: them), and the ``SPACE_ROUTED`` wrapper — it comes from the relay, and
#: the inner event it unwraps is dispatched on its own, under its origin.
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
#: Whether a held event may still be applied.
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


@dataclass(slots=True)
class _Held:
    """One event in a sender's queue (never leaves this module)."""

    event: FederationEvent
    size: int
    #: The handlers to run again; ``None`` — it has not been dispatched yet.
    failed: list[Failed] | None


@dataclass(slots=True)
class _Lane:
    """One sender's queue: the head is retried, the rest wait in order."""

    run: Runner
    queue: deque[_Held]
    #: Set to end the waiting: the queue overflowed, or we are stopping.
    hurry: asyncio.Event
    task: asyncio.Task[None] | None = None


class InboundRetry:
    """Bounded in-memory second tries, one ordered queue per sender."""

    __slots__ = (
        "_bytes",
        "_delays",
        "_dropped",
        "_entries",
        "_gate",
        "_lanes",
        "_max_bytes",
        "_max_entries",
        "_max_per_sender",
        "_on_applied",
        "_stop",
    )

    def __init__(
        self,
        *,
        gate: RetryGate | None = None,
        on_applied: Callable[[FederationEvent], None] | None = None,
        delays: tuple[float, ...] = RETRY_DELAYS_S,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_per_sender: int = DEFAULT_MAX_PER_SENDER,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self._gate = gate
        self._on_applied = on_applied
        self._delays = delays
        self._max_entries = max_entries
        self._max_per_sender = max_per_sender
        self._max_bytes = max_bytes
        self._entries = 0
        self._bytes = 0
        self._dropped = 0
        self._lanes: dict[str, _Lane] = {}
        self._stop = asyncio.Event()

    def __len__(self) -> int:
        return self._entries

    @property
    def held_bytes(self) -> int:
        """Bytes currently held (the ``max_bytes`` budget's fill)."""
        return self._bytes

    # ── What the registry asks ───────────────────────────────────────────

    def may_retry(self, event: FederationEvent, exc: BaseException) -> bool:
        """Whether a handler that raised ``exc`` on ``event`` gets a second
        try (bounds permitting — :meth:`hold` decides that)."""
        return (
            not self._stop.is_set()
            and is_transient(exc)
            and event.event_type not in NEVER_RETRIED
        )

    def defer(self, event: FederationEvent) -> bool:
        """``True`` when ``event`` was taken instead of being dispatched now:
        its sender has a held event, so it waits its turn behind it."""
        lane = self._lanes.get(event.from_instance)
        if lane is None or self._stop.is_set() or event.event_type in NEVER_RETRIED:
            return False
        size = self._size(event)
        if size is None or not self._fits(lane, size):
            if lane.hurry.is_set():
                log.warning(
                    "%s from %s: its retry queue is still full — dropped",
                    event.event_type,
                    event.from_instance,
                )
                return True
            log.warning(
                "%s: the retry queue is full — the wait ends, its %d queued"
                " events run now, in order",
                event.from_instance,
                len(lane.queue),
            )
            lane.hurry.set()
        self._add(lane, _Held(event, size or 0, None))
        return True

    def hold(self, event: FederationEvent, failed: list[Failed], run: Runner) -> bool:
        """Handlers ``failed`` on ``event`` just now (each
        :meth:`may_retry`): queue their second tries. ``False`` — the caller
        logs the failures as final — when the event cannot be held."""
        if self._stop.is_set():
            return False
        sender = event.from_instance
        size = self._size(event)
        lane = self._lanes.get(sender)
        if size is None or not self._fits(lane, size):
            log.warning(
                "%s from %s: a handler failed and the retry hold is full",
                event.event_type,
                sender,
            )
            return False
        if lane is None:
            lane = self._lanes[sender] = _Lane(run, deque(), asyncio.Event())
            lane.task = asyncio.create_task(self._drain(sender, lane))
        self._add(lane, _Held(event, size, failed))
        log.info(
            "%s from %s: %d handler(s) failed (%r) — trying again shortly",
            event.event_type,
            sender,
            len(failed),
            failed[-1][1],
        )
        return True

    async def stop(self) -> None:
        """Refuse new holds, let a handler that is running finish (never cut
        mid-write) and drop everything else, with one log line. Idempotent."""
        self._stop.set()
        lanes = list(self._lanes.values())
        for lane in lanes:
            lane.hurry.set()
        tasks = [lane.task for lane in lanes if lane.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._dropped:
            log.warning("%d held inbound event(s) dropped at shutdown", self._dropped)
            self._dropped = 0

    # ── Bookkeeping ──────────────────────────────────────────────────────

    @staticmethod
    def _size(event: FederationEvent) -> int | None:
        try:
            size = len(orjson.dumps(event.payload))
        except orjson.JSONEncodeError:
            # Nested deeper than orjson writes: cannot be sized.
            return None
        return size + len(event.media_bytes or b"")

    def _fits(self, lane: _Lane | None, size: int) -> bool:
        return (
            self._entries < self._max_entries
            and (lane is None or len(lane.queue) < self._max_per_sender)
            and self._bytes + size <= self._max_bytes
        )

    def _add(self, lane: _Lane, held: _Held) -> None:
        lane.queue.append(held)
        self._entries += 1
        self._bytes += held.size

    def _release(self, held: _Held) -> None:
        self._entries -= 1
        self._bytes -= held.size

    # ── The per-sender worker ────────────────────────────────────────────

    async def _drain(self, sender: str, lane: _Lane) -> None:
        try:
            while lane.queue and not self._stop.is_set():
                held = lane.queue[0]
                if not await self._settle(lane, held):
                    break  # stopping: it stays queued and is dropped below
                lane.queue.popleft()
                self._release(held)
        finally:
            for held in lane.queue:
                self._release(held)
            self._dropped += len(lane.queue)
            lane.queue.clear()
            if self._lanes.get(sender) is lane:
                del self._lanes[sender]

    async def _settle(self, lane: _Lane, held: _Held) -> bool:
        """Bring the head of a queue to its end — applied, refused or given
        up. ``False`` only when :meth:`stop` cut its wait short."""
        event = held.event
        tries = 1
        if held.failed is None:  # waited behind another: its first run
            if not await self._allowed(event):
                return True
            held.failed = await self._run(lane, event, None)
        for delay in self._delays:
            if not held.failed or lane.hurry.is_set():
                break
            try:
                await asyncio.wait_for(lane.hurry.wait(), timeout=delay)
            except TimeoutError:
                pass
            if self._stop.is_set():
                return False
            if lane.hurry.is_set():
                break
            tries += 1
            if not await self._allowed(event):
                return True
            held.failed = await self._run(lane, event, [h for h, _ in held.failed])
        if held.failed:
            log.warning(
                "%s from %s: %d handler(s) still failing after %d tries — the"
                " event is dropped",
                event.event_type,
                event.from_instance,
                len(held.failed),
                tries,
                exc_info=held.failed[-1][1],
            )
            return True
        if tries > 1:
            log.info(
                "%s from %s: applied on try %d",
                event.event_type,
                event.from_instance,
                tries,
            )
        if self._on_applied is not None:
            self._on_applied(event)
        return True

    async def _run(
        self,
        lane: _Lane,
        event: FederationEvent,
        handlers: Sequence[EventHandler] | None,
    ) -> list[Failed]:
        """Run handlers; the failures worth another try. Others are final."""
        again: list[Failed] = []
        for handler, exc in await lane.run(event, handlers):
            if is_transient(exc):
                again.append((handler, exc))
            else:
                log_handler_failure(event, handler, exc)
        return again

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
                "%s from %s: no longer allowed — dropped from the retry queue",
                event.event_type,
                event.from_instance,
            )
        return allowed
