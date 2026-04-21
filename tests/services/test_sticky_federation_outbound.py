"""StickyFederationOutbound — per-event outbound fan-out for space stickies."""

from __future__ import annotations

import pytest

from social_home.domain.events import (
    StickyCreated,
    StickyDeleted,
    StickyUpdated,
)
from social_home.domain.federation import FederationEventType
from social_home.infrastructure.event_bus import EventBus
from social_home.services.sticky_federation_outbound import (
    StickyFederationOutbound,
)


class _FakeFederationService:
    def __init__(self, own_instance_id: str = "own-inst") -> None:
        self._own_instance_id = own_instance_id
        self.sent: list[tuple[str, FederationEventType, dict, str | None]] = []

    async def send_event(
        self,
        *,
        to_instance_id,
        event_type,
        payload,
        space_id=None,
    ):
        self.sent.append((to_instance_id, event_type, payload, space_id))
        return None


class _FakeSpaceRepo:
    def __init__(self, members: dict[str, list[str]]) -> None:
        self._members = members

    async def list_member_instances(self, space_id: str) -> list[str]:
        return list(self._members.get(space_id, []))


@pytest.fixture
def env():
    bus = EventBus()
    fed = _FakeFederationService()
    repo = _FakeSpaceRepo(
        {
            "sp-A": ["own-inst", "peer-1", "peer-2"],
            "sp-B": ["peer-3"],
        }
    )
    out = StickyFederationOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=repo,
    )
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
    assert fed.sent == []


async def test_space_sticky_fanouts_to_peers_excluding_self(env):
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
    # Fan-out to every peer that isn't us.
    recipients = [r[0] for r in fed.sent]
    assert recipients == ["peer-1", "peer-2"]
    # All carry the SPACE_STICKY_CREATED type.
    assert {r[1] for r in fed.sent} == {FederationEventType.SPACE_STICKY_CREATED}
    # Payload shape: id replaces sticky_id, occurred_at stripped.
    payload = fed.sent[0][2]
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
    assert [r[1] for r in fed.sent] == [FederationEventType.SPACE_STICKY_UPDATED]
    assert fed.sent[0][0] == "peer-3"


async def test_sticky_deleted_payload_minimal(env):
    bus, fed = env
    await bus.publish(StickyDeleted(sticky_id="s1", space_id="sp-A"))
    assert len(fed.sent) == 2
    for _to, event_type, payload, space_id in fed.sent:
        assert event_type is FederationEventType.SPACE_STICKY_DELETED
        assert payload == {"id": "s1", "space_id": "sp-A"}
        assert space_id == "sp-A"
