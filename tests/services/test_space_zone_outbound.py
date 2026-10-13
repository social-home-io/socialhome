"""Tests for ``socialhome.services.space_zone_outbound`` (§23.8.7).

Pin the federation fan-out shape: SpaceZoneUpserted →
SPACE_ZONE_UPSERTED, SpaceZoneDeleted → SPACE_ZONE_DELETED, both via
mesh-routed `broadcast_to_space_members` so members behind a relay
also receive the events (F2a refactor).
"""

from __future__ import annotations

from typing import Any

import pytest

from socialhome.domain.events import SpaceZoneDeleted, SpaceZoneUpserted
from socialhome.domain.federation import FederationEventType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.space_zone_outbound import SpaceZoneOutbound


class _FakeFederation:
    def __init__(self, *, raise_on_broadcast: bool = False) -> None:
        self.broadcasts: list[dict[str, Any]] = []
        self._raise = raise_on_broadcast

    async def broadcast_to_space_members(
        self,
        space_id: str,
        event_type: FederationEventType,
        payload: dict,
        **kwargs: Any,
    ) -> None:
        if self._raise:
            raise RuntimeError("simulated transport failure")
        self.broadcasts.append(
            {
                "space_id": space_id,
                "event_type": event_type,
                "payload": payload,
            },
        )


@pytest.fixture
async def env():
    bus = EventBus()
    fed = _FakeFederation()
    outbound = SpaceZoneOutbound(bus=bus, federation_service=fed)
    outbound.wire()

    class E:
        pass

    e = E()
    e.bus = bus
    e.fed = fed
    return e


def _upsert_event(
    zone_id: str = "z_office", icon: str | None = None
) -> SpaceZoneUpserted:
    return SpaceZoneUpserted(
        space_id="sp_test",
        zone_id=zone_id,
        name="Office",
        latitude=47.3769,
        longitude=8.5417,
        radius_m=150,
        color="#3b82f6",
        created_by="u_admin",
        updated_at="2026-04-28T12:00:00+00:00",
        icon=icon,
    )


# ─── Upsert ─────────────────────────────────────────────────────────────


async def test_upsert_broadcasts_to_space_members(env):
    await env.bus.publish(_upsert_event())
    assert len(env.fed.broadcasts) == 1
    call = env.fed.broadcasts[0]
    assert call["space_id"] == "sp_test"
    assert call["event_type"] == FederationEventType.SPACE_ZONE_UPSERTED
    p = call["payload"]
    assert p["zone_id"] == "z_office"
    assert p["name"] == "Office"
    assert p["radius_m"] == 150
    assert p["color"] == "#3b82f6"
    assert "icon" in p and p["icon"] is None


async def test_upsert_carries_the_icon(env):
    """The icon rides the sealed payload; ``None`` is sent explicitly so a
    receiver clears its stored icon (absence means "keep" — older senders)."""
    await env.bus.publish(_upsert_event(icon="🏫"))
    assert env.fed.broadcasts[0]["payload"]["icon"] == "🏫"


# ─── Delete ─────────────────────────────────────────────────────────────


async def test_delete_broadcasts_with_correct_event_type(env):
    await env.bus.publish(
        SpaceZoneDeleted(
            space_id="sp_test",
            zone_id="z_office",
            deleted_by="u_admin",
        ),
    )
    assert len(env.fed.broadcasts) == 1
    call = env.fed.broadcasts[0]
    assert call["event_type"] == FederationEventType.SPACE_ZONE_DELETED
    assert call["payload"]["zone_id"] == "z_office"
    assert call["payload"]["deleted_by"] == "u_admin"


# ─── Defensive paths ────────────────────────────────────────────────────


async def test_broadcast_failure_is_swallowed():
    """A broadcast failure must not bubble up through the bus."""
    bus = EventBus()
    fed = _FakeFederation(raise_on_broadcast=True)
    SpaceZoneOutbound(bus=bus, federation_service=fed).wire()
    # Must not raise.
    await bus.publish(_upsert_event())
    assert fed.broadcasts == []
