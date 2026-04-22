"""In-process domain event bus (§5.2 pattern ①).

Services emit :class:`~socialhome.domain.events.DomainEvent` values by
calling :meth:`EventBus.publish`. Subscribers are called sequentially in
subscription order under the same asyncio event loop — no threads, no
queues, no ordering surprises within a single ``publish`` call.

**Failure semantics.** A handler that raises does NOT roll back the
originating mutation (the database write has already committed by the time
``publish`` is called). The bus catches and logs each handler's exception
independently so one failing subscriber does not block the rest.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import TypeVar

from ..domain.events import DomainEvent


log = logging.getLogger(__name__)


E = TypeVar("E", bound=DomainEvent)

#: Subscriber signature — any ``async def handler(event) -> None``.
Handler = Callable[[E], Awaitable[None]]


class EventBus:
    """Per-process synchronous event dispatcher.

    Handlers are stored indexed by the concrete event class. Publishing a
    subclass of ``DomainEvent`` does NOT trigger handlers registered on a
    parent class — registration is exact-match. This keeps dispatch cheap
    and subscribers explicit.
    """

    __slots__ = ("_handlers",)

    def __init__(self) -> None:
        self._handlers: dict[type[DomainEvent], list[Handler]] = defaultdict(list)

    def subscribe(
        self,
        event_type: type[E],
        handler: "Handler[E]",
    ) -> None:
        """Register ``handler`` for ``event_type``.

        Handlers are called in registration order. The same handler may be
        registered more than once; it will then be invoked once per
        registration.
        """
        self._handlers[event_type].append(handler)

    def unsubscribe(
        self,
        event_type: type[E],
        handler: "Handler[E]",
    ) -> None:
        """Remove ``handler`` from ``event_type``.

        No-op if the subscription doesn't exist. Useful when a subscriber
        (e.g. a long-running WS connection) goes away.
        """
        try:
            self._handlers[event_type].remove(handler)
        except KeyError, ValueError:
            return

    async def publish(self, event: DomainEvent) -> None:
        """Deliver ``event`` to all handlers registered on its exact type.

        Handler exceptions are logged and swallowed so one failing
        subscriber cannot starve others.
        """
        handlers = self._handlers.get(type(event), ())
        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                log.exception(
                    "EventBus handler %s failed for %s",
                    getattr(handler, "__qualname__", repr(handler)),
                    type(event).__name__,
                )

    def handler_count(self, event_type: type[DomainEvent]) -> int:
        """Number of handlers registered for ``event_type`` (useful in tests)."""
        return len(self._handlers.get(event_type, ()))

    def clear(self) -> None:
        """Drop all subscriptions. Useful between tests."""
        self._handlers.clear()
