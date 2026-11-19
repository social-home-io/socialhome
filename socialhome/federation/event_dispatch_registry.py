"""Event dispatch registry for federation events (Registry pattern).

The :class:`EventDispatchRegistry` eliminates the need for verbose
``if service is not None`` checks scattered throughout the dispatch logic.
Services register handlers for specific event types; dispatching becomes
a simple lookup and invoke.

This decouples the federation service from individual handlers, making it
easy to add new event types without editing :meth:`FederationService._dispatch_event`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from ..domain.federation import FederationEvent, FederationEventType
from .inbound_retry import InboundRetry

log = logging.getLogger(__name__)

#: Handler signature — takes a FederationEvent and returns an awaitable.
EventHandler = Callable[[FederationEvent], Awaitable[None]]


class EventDispatchRegistry:
    """Registry for federation event handlers.

    Handlers are stored indexed by event type. Calling :meth:`dispatch`
    looks up and invokes all handlers for that event type in registration
    order. Missing event types are a no-op (unlike the if/elif chain which
    logs "unhandled").

    This pattern replaces dense if/elif chains and eliminates per-handler
    None checks — services register themselves once, and the dispatcher
    just invokes.
    """

    __slots__ = ("_handlers", "_retry")

    def __init__(self, *, retry: InboundRetry | None = None) -> None:
        self._handlers: dict[FederationEventType, list[EventHandler]] = {}
        #: One more run for a handler that failed on a locked database.
        self._retry = retry

    def register(
        self,
        event_type: FederationEventType,
        handler: EventHandler,
    ) -> None:
        """Register a handler for a specific event type.

        Handlers are called in registration order. The same handler can be
        registered for multiple event types.
        """
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    def unregister(
        self,
        event_type: FederationEventType,
        handler: EventHandler,
    ) -> None:
        """Remove a handler for an event type.

        No-op if the handler isn't registered. Useful when a handler
        (e.g. a long-running service) goes away.
        """
        if event_type in self._handlers:
            try:
                self._handlers[event_type].remove(handler)
            except ValueError:
                pass

    async def dispatch(self, event: FederationEvent) -> None:
        """Invoke all handlers registered for the event's type.

        A handler exception never blocks the other handlers. The envelope's
        replay id is already stored, so the sender cannot deliver it again:
        a handler that failed on a locked database gets one more run in
        place (:class:`InboundRetry`). Every failure that is final is one
        ERROR record naming the handler, the event type, the sending
        household and the exception class, with the traceback — the
        message itself never carries the payload.
        """
        for handler in tuple(self._handlers.get(event.event_type, ())):
            try:
                await handler(event)
            except Exception as first:
                exc: Exception | None = first
                if self._retry is not None:
                    exc = await self._retry.again(event, handler, first)
                if exc is None:
                    continue
                name = getattr(handler, "__qualname__", repr(handler))
                log.error(
                    "Event handler %s failed for %s from %s: %s — the event"
                    " is lost for this handler",
                    name,
                    event.event_type.value,
                    event.from_instance,
                    type(exc).__name__,
                    exc_info=exc,
                )

    def handlers_for(
        self,
        event_type: FederationEventType,
    ) -> tuple[EventHandler, ...]:
        """Every handler bound to ``event_type``, in registration order.

        :meth:`dispatch` runs all of them, so "who applies this event?" is
        a question with more than one possible answer — and a guarded
        handler does NOT shadow an unguarded sibling registered next to
        it. The roster-authority protocol tests enumerate this so a
        future duplicate fails there rather than in the wild.
        """
        return tuple(self._handlers.get(event_type, ()))

    def handler_count(self, event_type: FederationEventType) -> int:
        """Number of handlers registered for an event type (useful in tests)."""
        return len(self._handlers.get(event_type, ()))

    def clear(self) -> None:
        """Drop all handlers. Useful between tests."""
        self._handlers.clear()
