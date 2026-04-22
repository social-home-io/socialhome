"""Tests for :class:`PairingRelayQueue` (§11.9)."""

from __future__ import annotations

from types import SimpleNamespace


from socialhome.domain.events import PairingIntroRelayReceived
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.pairing_relay_queue import (
    PairingRelayQueue,
    _MAX_QUEUE_SIZE,
)


class _FakeFederation:
    """Captures send_event calls for assertion in approve() tests."""

    def __init__(self, *, ok: bool = True) -> None:
        self.calls: list[dict] = []
        self._ok = ok

    async def send_event(self, *, to_instance_id, event_type, payload):
        self.calls.append(
            {
                "to": to_instance_id,
                "type": event_type,
                "payload": payload,
            }
        )
        return SimpleNamespace(ok=self._ok)


async def test_queue_collects_relay_events_from_bus():
    bus = EventBus()
    fed = _FakeFederation()
    q = PairingRelayQueue(bus=bus, federation=fed, own_instance_id="self")
    q.wire()

    await bus.publish(
        PairingIntroRelayReceived(
            from_instance="peer-a",
            target_instance_id="peer-b",
            message="please intro",
        )
    )
    pending = q.list_pending()
    assert len(pending) == 1
    assert pending[0].from_instance == "peer-a"
    assert pending[0].target_instance_id == "peer-b"
    assert pending[0].message == "please intro"


async def test_approve_forwards_pairing_intro_to_target():
    bus = EventBus()
    fed = _FakeFederation()
    q = PairingRelayQueue(bus=bus, federation=fed, own_instance_id="self")
    q.wire()

    await bus.publish(
        PairingIntroRelayReceived(
            from_instance="peer-a",
            target_instance_id="peer-b",
            message="hi",
        )
    )
    [req] = q.list_pending()

    approved = await q.approve(req.id)
    assert approved is not None
    assert q.get(req.id) is None
    assert len(fed.calls) == 1
    call = fed.calls[0]
    assert call["to"] == "peer-b"
    assert call["payload"]["via_instance_id"] == "peer-a"


async def test_decline_drops_without_sending():
    bus = EventBus()
    fed = _FakeFederation()
    q = PairingRelayQueue(bus=bus, federation=fed, own_instance_id="self")
    q.wire()

    await bus.publish(
        PairingIntroRelayReceived(
            from_instance="peer-a",
            target_instance_id="peer-b",
        )
    )
    [req] = q.list_pending()

    dropped = q.decline(req.id)
    assert dropped is not None
    assert q.list_pending() == []
    assert fed.calls == []


async def test_unknown_id_returns_none():
    bus = EventBus()
    fed = _FakeFederation()
    q = PairingRelayQueue(bus=bus, federation=fed, own_instance_id="self")
    q.wire()

    assert await q.approve("no-such-id") is None
    assert q.decline("no-such-id") is None


async def test_queue_caps_at_max_size():
    bus = EventBus()
    fed = _FakeFederation()
    q = PairingRelayQueue(bus=bus, federation=fed, own_instance_id="self")
    q.wire()

    for i in range(_MAX_QUEUE_SIZE + 5):
        await bus.publish(
            PairingIntroRelayReceived(
                from_instance=f"peer-{i}",
                target_instance_id="peer-t",
            )
        )
    assert len(q.list_pending()) == _MAX_QUEUE_SIZE
