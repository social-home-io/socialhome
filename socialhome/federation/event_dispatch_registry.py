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
from collections.abc import Awaitable, Callable, Sequence

from ..domain.federation import FederationEvent, FederationEventType
from .inbound_retry import NEVER_RETRIED, Failed, InboundRetry, log_handler_failure

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
        #: Per-sender lanes and the in-place second tries.
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

        A handler exception never blocks the other handlers. With an
        :class:`InboundRetry` the event runs in its sender's lane — one
        household's events are applied in arrival order — and a handler
        that failed for a passing reason is run again there (the envelope's
        replay id is already stored, so the sender cannot deliver it
        again). Any other failure is logged and the event is dropped for
        that handler. Returns once the handlers are done with the event.
        """
        if self._retry is not None and event.event_type not in NEVER_RETRIED:
            await self._retry.dispatch(event, self._run)
            return
        for handler, exc in await self._run(event, None):
            log_handler_failure(event, handler, exc)

    async def _run(
        self,
        event: FederationEvent,
        handlers: Sequence[EventHandler] | None,
    ) -> list[Failed]:
        """Run ``handlers`` (``None``: every handler of the event's type) in
        order; return the ones that raised, with what they raised."""
        if handlers is None:
            handlers = tuple(self._handlers.get(event.event_type, ()))
        failed: list[Failed] = []
        for handler in handlers:
            try:
                await handler(event)
            except Exception as exc:  # noqa: BLE001 — judged by the caller
                failed.append((handler, exc))
        return failed

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
