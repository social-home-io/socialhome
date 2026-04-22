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

    __slots__ = ("_handlers",)

    def __init__(self) -> None:
        self._handlers: dict[FederationEventType, list[EventHandler]] = {}

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

        Handler exceptions are logged and swallowed so one failing
        handler doesn't block others.
        """
        handlers = self._handlers.get(event.event_type, ())
        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                log.exception(
                    "Event handler %s failed for %s",
                    getattr(handler, "__qualname__", repr(handler)),
                    event.event_type,
                )

    def handler_count(self, event_type: FederationEventType) -> int:
        """Number of handlers registered for an event type (useful in tests)."""
        return len(self._handlers.get(event_type, ()))

    def clear(self) -> None:
        """Drop all handlers. Useful between tests."""
        self._handlers.clear()
