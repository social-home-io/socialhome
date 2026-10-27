"""Unit tests for :class:`OrderedPublisher` (per-key FIFO publication)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from socialhome.domain.events import DomainEvent
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.ordered_publisher import OrderedPublisher


@dataclass(frozen=True)
class _Ev(DomainEvent):
    name: str = ""


def _setup():
    bus = EventBus()
    seen: list[str] = []
    gates: dict[str, asyncio.Event] = {}

    async def handler(e: _Ev) -> None:
        gate = gates.get(e.name)
        if gate is not None:
            await gate.wait()
        seen.append(e.name)

    bus.subscribe(_Ev, handler)
    return OrderedPublisher(bus), seen, gates


async def test_a_lone_event_is_published_before_the_waiter_resolves():
    pub, seen, _ = _setup()
    waiter = pub.enqueue("k", _Ev(name="a"))
    assert waiter is not None
    await asyncio.shield(waiter)
    assert seen == ["a"]


async def test_a_later_event_does_not_wait_for_a_slow_publish():
    pub, seen, gates = _setup()
    gates["a"] = asyncio.Event()  # a slow handler: a dead household
    first = pub.enqueue("k", _Ev(name="a"))
    await asyncio.sleep(0)
    # The second writer gets no waiter — it returns at once.
    assert pub.enqueue("k", _Ev(name="b")) is None
    assert seen == []
    assert pub.pending("k") == 1
    gates["a"].set()
    await first
    await pub.flush()
    assert seen == ["a", "b"]


async def test_order_holds_per_key_and_keys_are_independent():
    pub, seen, gates = _setup()
    gates["a1"] = asyncio.Event()
    pub.enqueue("a", _Ev(name="a1"))
    pub.enqueue("a", _Ev(name="a2"))
    pub.enqueue("a", _Ev(name="a3"))
    other = pub.enqueue("b", _Ev(name="b1"))
    assert other is not None
    await asyncio.wait_for(asyncio.shield(other), 1)
    assert seen == ["b1"]  # another key never waits on "a"
    gates["a1"].set()
    await pub.flush()
    assert seen == ["b1", "a1", "a2", "a3"]


async def test_publish_waits_only_when_idle():
    pub, seen, gates = _setup()
    gates["a"] = asyncio.Event()
    first = asyncio.create_task(pub.publish("k", _Ev(name="a")))
    await asyncio.sleep(0)
    await asyncio.wait_for(pub.publish("k", _Ev(name="b")), 1)
    assert not first.done()
    gates["a"].set()
    await first
    await pub.flush()
    assert seen == ["a", "b"]


async def test_a_cancelled_caller_does_not_drop_its_event():
    pub, seen, gates = _setup()
    gates["a"] = asyncio.Event()
    caller = asyncio.create_task(pub.publish("k", _Ev(name="a")))
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    gates["a"].set()
    await pub.flush()
    assert seen == ["a"]


async def test_a_failing_handler_does_not_stop_the_queue():
    bus = EventBus()
    seen: list[str] = []

    async def handler(e: _Ev) -> None:
        if e.name == "boom":
            raise RuntimeError("down")
        seen.append(e.name)

    bus.subscribe(_Ev, handler)
    pub = OrderedPublisher(bus)
    pub.enqueue("k", _Ev(name="boom"))
    pub.enqueue("k", _Ev(name="ok"))
    await pub.flush()
    assert seen == ["ok"]


async def test_the_key_is_idle_again_after_draining():
    pub, seen, _ = _setup()
    await pub.publish("k", _Ev(name="a"))
    assert pub.pending("k") == 0
    assert pub.enqueue("k", _Ev(name="b")) is not None
    await pub.flush()
    assert seen == ["a", "b"]


async def test_stop_waits_then_cancels_and_releases_waiters(caplog):
    pub, seen, gates = _setup()
    gates["a"] = asyncio.Event()  # never opens
    waiter = pub.enqueue("k", _Ev(name="a"))
    pub.enqueue("k", _Ev(name="b"))
    await asyncio.sleep(0)
    await pub.stop(timeout=0.05)
    assert waiter is not None and waiter.done()
    assert seen == []
    assert "1 event(s) unpublished" in caplog.text
    # Usable again afterwards.
    await pub.publish("k", _Ev(name="c"))
    assert seen == ["c"]


async def test_stop_with_nothing_queued_returns():
    pub, _, _ = _setup()
    await pub.stop(timeout=0.05)
    await pub.flush()
