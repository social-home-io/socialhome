"""One more try for an inbound handler that failed on a locked database.

An inbound envelope is verified, its ``msg_id`` is stored in the replay
cache, and only then are its handlers run (§24.11). A handler that fails
after that loses the event: the sender was told (or assumed) the delivery
worked, and a re-send of the same envelope is refused as a replay.

:class:`InboundRetry` gives a handler that raised ``database is locked`` /
``busy`` (:func:`is_transient`) exactly one more run, in place —
:meth:`~socialhome.federation.event_dispatch_registry.EventDispatchRegistry.dispatch`
awaits it — :data:`RETRY_DELAY_S` after the failure, once the gate (still a
peer, the post-decrypt gates) passed again. Such a failure happened at a
write that did not commit, so running the handler again repeats nothing.
It is this household's own second try of an envelope it already verified:
the replay cache is not touched and nothing is asked of the sender.

Nothing else is tried again: any other exception (a refusal, a bug, a
timeout or I/O error — the handler may already have written something),
and the :data:`NEVER_RETRIED` events. Nothing is ordered: a later event of
the same sender that arrives on another connection can be applied before
the retried one. Nothing is stored: an event waiting for its second try
when the process dies is lost.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable

from ..domain.federation import FederationEvent, FederationEventType
from ..domain.federation_retention import SHORT_LIVED

log = logging.getLogger(__name__)

#: Seconds between the failure and the second try. Counted from the
#: failure: a lock held by another process surfaces only after SQLite's
#: busy timeout (5 s), and the second try waits that long again.
RETRY_DELAY_S: float = 1.0

#: How long :meth:`InboundRetry.stop` waits for the second tries under way.
STOP_WAIT_S: float = 10.0


def is_transient(exc: BaseException) -> bool:
    """Whether ``exc`` is SQLite refusing a write because the database was
    locked or busy — the one failure that passes on its own and that left
    nothing half-done (other ``OperationalError``s are schema or SQL bugs)."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return "locked" in message or "busy" in message


#: Events never tried again: a call that has stopped ringing
#: (:data:`~socialhome.domain.federation_retention.SHORT_LIVED`), a typing
#: indicator, transport signalling, the sync streams (stateful, with their
#: own resume), and the ``SPACE_ROUTED`` wrapper (its handler marks the
#: route seen first; the inner event it unwraps is dispatched on its own).
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
#: Whether an event may still be applied on its second try.
RetryGate = Callable[[FederationEvent], Awaitable[bool]]


class InboundRetry:
    """The in-place second try of a failed inbound handler."""

    __slots__ = ("_delay", "_gate", "_idle", "_stop", "_waiting")

    def __init__(
        self, *, gate: RetryGate | None = None, delay: float = RETRY_DELAY_S
    ) -> None:
        self._gate = gate
        self._delay = delay
        self._waiting = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._stop = asyncio.Event()

    async def again(
        self, event: FederationEvent, handler: EventHandler, exc: Exception
    ) -> Exception | None:
        """``handler`` just raised ``exc`` on ``event``. Returns the failure
        that is final — ``exc`` itself when it gets no second try, what the
        second try raised, or what the gate raised — or ``None`` when the
        second try applied the event or the gate no longer allows it."""
        if not is_transient(exc) or event.event_type in NEVER_RETRIED:
            return exc
        self._waiting += 1
        self._idle.clear()
        try:
            log.info(
                "%s from %s: the database was locked — one more try in %ss",
                event.event_type,
                event.from_instance,
                self._delay,
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._delay)
            except TimeoutError:
                pass  # the delay is over; a set ``_stop`` only ends it early
            try:
                if self._gate is not None and not await self._gate(event):
                    log.info(
                        "%s from %s: no longer allowed — not tried again",
                        event.event_type,
                        event.from_instance,
                    )
                    return None
                await handler(event)
            except Exception as again:  # noqa: BLE001 — returned: the caller logs it
                return again
            return None
        finally:
            self._waiting -= 1
            if not self._waiting:
                self._idle.set()

    async def stop(self) -> None:
        """End every wait: each event waiting for its second try gets it now
        (a handler is never cancelled). Idempotent."""
        self._stop.set()
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=STOP_WAIT_S)
        except TimeoutError:
            log.warning(
                "%d inbound event(s) still in their second try at shutdown",
                self._waiting,
            )
