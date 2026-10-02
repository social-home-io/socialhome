"""StickyFederationOutbound — per-event outbound fan-out for space stickies."""

from __future__ import annotations

import pytest

from socialhome.domain.events import (
    StickyCreated,
    StickyDeleted,
    StickyUpdated,
)
from socialhome.domain.federation import FederationEventType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.sticky_federation_outbound import (
    StickyFederationOutbound,
)


class _FakeFederationService:
    """Stub that records `broadcast_to_space_members` calls.

    Switched from per-peer `send_event` to mesh-routed broadcast in the
    F2a refactor — the broadcast helper iterates members internally and
    uses `send_with_mesh_fallback`, so mesh-only members get the event
    too. The test assertion is on the broadcast call, not on individual
    per-peer sends.
    """

    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, FederationEventType, dict]] = []

    async def broadcast_to_space_members(
        self,
        space_id,
        event_type,
        payload,
        **kwargs,
    ):
        self.broadcasts.append((space_id, event_type, payload))
        return None


@pytest.fixture
def env():
    bus = EventBus()
    fed = _FakeFederationService()
    out = StickyFederationOutbound(bus=bus, federation_service=fed)
    out.wire()
    return bus, fed


async def test_household_sticky_is_not_federated(env):
    bus, fed = env
    await bus.publish(
        StickyCreated(
            sticky_id="s1",
            space_id=None,
            author="u",
            content="x",
            color="#FFF9B1",
            position_x=0,
            position_y=0,
        )
    )
    assert fed.broadcasts == []


async def test_space_sticky_broadcasts_with_payload(env):
    bus, fed = env
    await bus.publish(
        StickyCreated(
            sticky_id="s1",
            space_id="sp-A",
            author="u",
            content="x",
            color="#FFF9B1",
            position_x=10,
            position_y=20,
        )
    )
    assert len(fed.broadcasts) == 1
    space_id, event_type, payload = fed.broadcasts[0]
    assert space_id == "sp-A"
    assert event_type is FederationEventType.SPACE_STICKY_CREATED
    # Payload shape: id replaces sticky_id, occurred_at stripped.
    assert payload["id"] == "s1"
    assert "sticky_id" not in payload
    assert "occurred_at" not in payload
    assert payload["space_id"] == "sp-A"
    assert payload["position_x"] == 10


async def test_sticky_updated_uses_update_event_type(env):
    bus, fed = env
    await bus.publish(
        StickyUpdated(
            sticky_id="s1",
            space_id="sp-B",
            content="y",
            color="#B3FFB3",
            position_x=5,
            position_y=5,
        )
    )
    assert len(fed.broadcasts) == 1
    space_id, event_type, _payload = fed.broadcasts[0]
    assert space_id == "sp-B"
    assert event_type is FederationEventType.SPACE_STICKY_UPDATED


async def test_sticky_deleted_payload_minimal(env):
    bus, fed = env
    await bus.publish(StickyDeleted(sticky_id="s1", space_id="sp-A"))
    assert len(fed.broadcasts) == 1
    space_id, event_type, payload = fed.broadcasts[0]
    assert event_type is FederationEventType.SPACE_STICKY_DELETED
    assert space_id == "sp-A"
    assert payload == {"id": "s1", "space_id": "sp-A"}


async def test_every_write_carries_its_actor(env):
    """v_42: the actor rides inside the sealed payload so receivers can
    judge the write against the space's ``stickies`` access level."""
    bus, fed = env
    await bus.publish(
        StickyCreated(
            sticky_id="s1",
            space_id="sp-A",
            author="u-a",
            content="x",
            color="#FFF9B1",
            position_x=0,
            position_y=0,
            actor_user_id="u-a",
        )
    )
    await bus.publish(
        StickyUpdated(
            sticky_id="s1",
            space_id="sp-A",
            content="y",
            color="#FFF9B1",
            position_x=0,
            position_y=0,
            actor_user_id="u-b",
        )
    )
    await bus.publish(
        StickyDeleted(sticky_id="s1", space_id="sp-A", actor_user_id="u-c")
    )
    assert [p["actor_user_id"] for _s, _t, p in fed.broadcasts] == [
        "u-a",
        "u-b",
        "u-c",
    ]
