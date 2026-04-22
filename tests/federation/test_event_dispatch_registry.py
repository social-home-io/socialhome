"""Tests for EventDispatchRegistry — federation event handler registry."""

from __future__ import annotations


from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.federation.event_dispatch_registry import EventDispatchRegistry


def _event(
    event_type: FederationEventType = FederationEventType.SPACE_POST_CREATED,
) -> FederationEvent:
    return FederationEvent(
        msg_id="m1",
        event_type=event_type,
        from_instance="remote",
        to_instance="self",
        timestamp="2026-01-01T00:00:00+00:00",
        payload={"text": "hi"},
    )


async def test_dispatch_invokes_registered_handler():
    reg = EventDispatchRegistry()
    called = []

    async def handler(event):
        called.append(event.msg_id)

    reg.register(FederationEventType.SPACE_POST_CREATED, handler)
    await reg.dispatch(_event())
    assert called == ["m1"]


async def test_dispatch_invokes_multiple_handlers_in_order():
    reg = EventDispatchRegistry()
    order = []

    async def first(event):
        order.append("first")

    async def second(event):
        order.append("second")

    reg.register(FederationEventType.SPACE_POST_CREATED, first)
    reg.register(FederationEventType.SPACE_POST_CREATED, second)
    await reg.dispatch(_event())
    assert order == ["first", "second"]


async def test_dispatch_noop_for_unregistered_event_type():
    reg = EventDispatchRegistry()
    await reg.dispatch(_event(FederationEventType.SPACE_MEMBER_JOINED))


async def test_handler_error_does_not_block_others():
    reg = EventDispatchRegistry()
    called = []

    async def failing(event):
        raise RuntimeError("boom")

    async def succeeding(event):
        called.append("ok")

    reg.register(FederationEventType.SPACE_POST_CREATED, failing)
    reg.register(FederationEventType.SPACE_POST_CREATED, succeeding)
    await reg.dispatch(_event())
    assert called == ["ok"]


async def test_unregister_removes_handler():
    reg = EventDispatchRegistry()
    called = []

    async def handler(event):
        called.append(1)

    reg.register(FederationEventType.SPACE_POST_CREATED, handler)
    reg.unregister(FederationEventType.SPACE_POST_CREATED, handler)
    await reg.dispatch(_event())
    assert called == []


async def test_unregister_noop_for_unknown_handler():
    reg = EventDispatchRegistry()

    async def handler(event):
        pass

    reg.unregister(FederationEventType.SPACE_POST_CREATED, handler)


async def test_handler_count():
    reg = EventDispatchRegistry()

    async def h1(event):
        pass

    async def h2(event):
        pass

    assert reg.handler_count(FederationEventType.SPACE_POST_CREATED) == 0
    reg.register(FederationEventType.SPACE_POST_CREATED, h1)
    assert reg.handler_count(FederationEventType.SPACE_POST_CREATED) == 1
    reg.register(FederationEventType.SPACE_POST_CREATED, h2)
    assert reg.handler_count(FederationEventType.SPACE_POST_CREATED) == 2


async def test_clear_drops_all_handlers():
    reg = EventDispatchRegistry()

    async def h(event):
        pass

    reg.register(FederationEventType.SPACE_POST_CREATED, h)
    reg.register(FederationEventType.SPACE_MEMBER_JOINED, h)
    reg.clear()
    assert reg.handler_count(FederationEventType.SPACE_POST_CREATED) == 0
    assert reg.handler_count(FederationEventType.SPACE_MEMBER_JOINED) == 0


async def test_same_handler_for_multiple_event_types():
    reg = EventDispatchRegistry()
    called = []

    async def handler(event):
        called.append(event.event_type)

    reg.register(FederationEventType.SPACE_POST_CREATED, handler)
    reg.register(FederationEventType.SPACE_MEMBER_JOINED, handler)
    await reg.dispatch(_event(FederationEventType.SPACE_POST_CREATED))
    await reg.dispatch(_event(FederationEventType.SPACE_MEMBER_JOINED))
    assert len(called) == 2
