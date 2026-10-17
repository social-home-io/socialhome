"""Shared event-publishing mixin for services that hold an optional bus.

Many domain services take an optional ``EventBus`` (``bus=None`` when the
service runs without live UI/WS fan-out, e.g. in unit tests or headless
jobs) and guard every publish with the same boilerplate::

    if self._bus is not None:
        await self._bus.publish(SomeEvent(...))

That guard recurs ~40 times across ~8 unrelated services (tasks,
shopping, calendar, polls, presence, preferences, space zones, space
crypto). This mixin owns the *behaviour* — a fail-soft ``_emit`` that
no-ops when no bus is wired — so call sites collapse to::

    await self._emit(SomeEvent(...))

Why a behaviour-only mixin (``__slots__ = ()``)
-----------------------------------------------
Unlike :class:`socialhome.services.visibility.VisibilityMixin`, this
mixin does NOT own the ``_bus`` slot. Python forbids inheriting from
more than one base that declares non-empty ``__slots__`` ("multiple
bases have instance lay-out conflict"). A service may need to combine
this with another slotted mixin (e.g. the planned ``SpaceMemberGuardMixin``
on ``space_zone_service``), so the composable shape is an empty-slots
mixin that reads the ``_bus`` slot the consuming service already
declares in its own ``__slots__``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..domain.events import DomainEvent
    from ..infrastructure.event_bus import EventBus


class BusPublisherMixin:
    """Mixin: publish domain events through an optional ``EventBus``.

    The consuming service declares ``_bus`` in its own ``__slots__`` and
    assigns it in ``__init__`` (``self._bus = bus``); this mixin only
    contributes the shared ``_emit`` reader, so it carries no slots of
    its own.
    """

    __slots__ = ()

    _bus: EventBus | None

    async def _emit(self, event: DomainEvent) -> None:
        """Publish ``event`` if a bus is wired; otherwise no-op.

        Mirrors the long-standing ``if self._bus is not None`` guard so a
        service constructed without a bus (tests, headless jobs) simply
        skips fan-out instead of raising.
        """
        if self._bus is not None:
            await self._bus.publish(event)
