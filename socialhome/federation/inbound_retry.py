"""Second tries for an inbound event whose handler failed for a passing reason.

An inbound envelope is verified, its ``msg_id`` is stored in the replay
cache, and only then are its handlers run (§24.11). A handler that fails
after that — the database was locked, a service was not attached yet at
startup — used to lose the event: the sender was told (or assumed) the
delivery worked, and a re-send of the same envelope is refused as a replay.
What the space sync carries came back at the next sync; a direct message, a
reaction, a pairing or key event did not.

:class:`InboundRetry` keeps such an event in memory and runs the handler
that failed — only that one — again after :data:`RETRY_DELAYS_S`. It is this
household's own second try of an envelope it has already verified; nothing
is asked of the sender and the replay cache is not touched, so a replayed
envelope is refused exactly as before. It sits behind
:meth:`~socialhome.federation.event_dispatch_registry.EventDispatchRegistry.dispatch`,
which every transport ends in (HTTPS inbox, DataChannel, GFS relay, GFS
mailbox and multi-recipient frames, mesh ``SPACE_ROUTED`` inner events).

What is never tried again:

* a failure that would repeat (:data:`PERMANENT_ERRORS` — a refusal or a
  programming error on this payload);
* :data:`NEVER_RETRIED` events — call negotiation, typing, transport
  signalling and the sync streams, which are stale within seconds or have
  their own resume;
* an event the post-decrypt gates no longer allow (a ban that landed
  meanwhile) — the gates run again before each try.

Bounded like the other inbound holds (count, count per sending household,
bytes); the tries end after ~26 s. In memory on purpose: a restart loses
what was held — the outcome before this existed, not a new one — and
:meth:`InboundRetry.stop` gives every held event one last try first.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable

import orjson

from ..domain.federation import FederationEvent, FederationEventType
from ..domain.federation_retention import SHORT_LIVED

log = logging.getLogger(__name__)

#: Seconds before the 2nd, 3rd and 4th try. Short on purpose: a lock or a
#: start-up race is over in seconds, and a try that late is no further out
#: of order than the sender's own outbox already delivers.
RETRY_DELAYS_S: tuple[float, ...] = (1.0, 5.0, 20.0)

DEFAULT_MAX_ENTRIES = 256
DEFAULT_MAX_PER_SENDER = 64
DEFAULT_MAX_BYTES = 4 * 1024 * 1024

#: A handler raising one of these fails the same way on every try: the
#: payload is malformed or refused (``ValueError`` and the lookup / type
#: errors a bad payload causes), not permitted, or already stored.
PERMANENT_ERRORS: tuple[type[BaseException], ...] = (
    ValueError,
    LookupError,
    TypeError,
    PermissionError,
    sqlite3.IntegrityError,
)

#: Events a late second try would only hurt: a call that has stopped
#: ringing (:data:`~socialhome.domain.federation_retention.SHORT_LIVED`), a
#: typing indicator, transport signalling, and the sync streams (stateful,
#: with their own resume).
NEVER_RETRIED: frozenset[FederationEventType] = (
    SHORT_LIVED
    | frozenset(
        {
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
#: Whether a held event may still be applied (the post-decrypt gates).
RetryGate = Callable[[FederationEvent], Awaitable[bool]]


def _name(handler: EventHandler) -> str:
    return getattr(handler, "__qualname__", repr(handler))


class InboundRetry:
    """Bounded in-memory second tries for failed inbound handlers."""

    __slots__ = (
        "_bytes",
        "_delays",
        "_gate",
        "_max_bytes",
        "_max_entries",
        "_max_per_sender",
        "_per_sender",
        "_stop",
        "_tasks",
    )

    def __init__(
        self,
        *,
        gate: RetryGate | None = None,
        delays: tuple[float, ...] = RETRY_DELAYS_S,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_per_sender: int = DEFAULT_MAX_PER_SENDER,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self._gate = gate
        self._delays = delays
        self._max_entries = max_entries
        self._max_per_sender = max_per_sender
        self._max_bytes = max_bytes
        self._bytes = 0
        self._per_sender: dict[str, int] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._stop = asyncio.Event()

    def __len__(self) -> int:
        return len(self._tasks)

    def hold(
        self, event: FederationEvent, handler: EventHandler, exc: BaseException
    ) -> bool:
        """``handler`` just raised ``exc`` on ``event``: schedule its second
        tries. ``False`` — the caller logs the failure as final — when the
        failure would repeat, the event is never retried, a bound is hit or
        we are stopping."""
        if (
            self._stop.is_set()
            or isinstance(exc, PERMANENT_ERRORS)
            or event.event_type in NEVER_RETRIED
        ):
            return False
        sender = event.from_instance
        size = len(orjson.dumps(event.payload, default=str)) + len(
            event.media_bytes or b""
        )
        if (
            len(self._tasks) >= self._max_entries
            or self._per_sender.get(sender, 0) >= self._max_per_sender
            or self._bytes + size > self._max_bytes
        ):
            log.warning(
                "%s from %s: handler %s failed and the retry hold is full",
                event.event_type,
                sender,
                _name(handler),
            )
            return False
        self._bytes += size
        self._per_sender[sender] = self._per_sender.get(sender, 0) + 1
        log.info(
            "%s from %s: handler %s failed (%r) — trying again in %ss",
            event.event_type,
            sender,
            _name(handler),
            exc,
            self._delays[0] if self._delays else 0,
        )
        task = asyncio.create_task(self._run(event, handler, size, exc))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def stop(self) -> None:
        """Refuse new holds, give every held event one last try now, and
        wait for those tries. Idempotent."""
        self._stop.set()
        tasks = list(self._tasks)
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=5.0)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _stopping_after(self, delay: float) -> bool:
        """Wait ``delay`` seconds; ``True`` when :meth:`stop` cut it short."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except TimeoutError:
            return False
        return True

    async def _run(
        self,
        event: FederationEvent,
        handler: EventHandler,
        size: int,
        last: BaseException,
    ) -> None:
        tries = 1
        try:
            for delay in self._delays:
                stopping = await self._stopping_after(delay)
                tries += 1
                try:
                    if self._gate is not None and not await self._gate(event):
                        log.info(
                            "%s from %s: no longer allowed — not tried again",
                            event.event_type,
                            event.from_instance,
                        )
                        return
                    await handler(event)
                except Exception as exc:  # noqa: BLE001 — judged below, logged at the end
                    last = exc
                    if stopping or isinstance(exc, PERMANENT_ERRORS):
                        break
                    continue
                log.info(
                    "%s from %s: handler %s applied it on try %d",
                    event.event_type,
                    event.from_instance,
                    _name(handler),
                    tries,
                )
                return
            log.warning(
                "%s from %s: handler %s still failing after %d tries — the"
                " event is dropped",
                event.event_type,
                event.from_instance,
                _name(handler),
                tries,
                exc_info=last,
            )
        finally:
            self._bytes -= size
            left = self._per_sender.get(event.from_instance, 1) - 1
            if left > 0:
                self._per_sender[event.from_instance] = left
            else:
                self._per_sender.pop(event.from_instance, None)
