"""Tests for ``socialhome.services.space_zone_outbound`` (§23.8.7).

Pin the federation fan-out shape: SpaceZoneUpserted →
SPACE_ZONE_UPSERTED, SpaceZoneDeleted → SPACE_ZONE_DELETED, sent to
every remote member instance and skipping the local own instance.
"""

from __future__ import annotations

from typing import Any

import pytest

from socialhome.domain.events import SpaceZoneDeleted, SpaceZoneUpserted
from socialhome.domain.federation import FederationEventType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.space_zone_outbound import SpaceZoneOutbound


class _FakeSpaceRepo:
    def __init__(self, peers: list[str]) -> None:
        self._peers = peers
        self.calls: list[str] = []

    async def list_member_instances(self, space_id: str) -> list[str]:
        self.calls.append(space_id)
        return list(self._peers)


class _FakeFederation:
    _own_instance_id = "self_instance"

    def __init__(self, raise_on: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raise_on = raise_on

    async def send_event(
        self,
        *,
        to_instance_id: str,
        event_type: FederationEventType,
        payload: dict,
        space_id: str | None = None,
    ) -> None:
        if self._raise_on is not None and to_instance_id == self._raise_on:
            raise RuntimeError("simulated transport failure")
        self.calls.append(
            {
                "to_instance_id": to_instance_id,
                "event_type": event_type,
                "payload": payload,
                "space_id": space_id,
            },
        )


@pytest.fixture
async def env():
    bus = EventBus()
    spaces = _FakeSpaceRepo(["self_instance", "remote_a", "remote_b"])
    fed = _FakeFederation()
    outbound = SpaceZoneOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=spaces,  # type: ignore[arg-type]
    )
    outbound.wire()

    class E:
        pass

    e = E()
    e.bus = bus
    e.spaces = spaces
    e.fed = fed
    return e


def _upsert_event(zone_id: str = "z_office") -> SpaceZoneUpserted:
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
    )


# ─── Upsert ─────────────────────────────────────────────────────────────


async def test_upsert_fans_to_remote_member_instances(env):
    await env.bus.publish(_upsert_event())
    assert len(env.fed.calls) == 2
    targets = sorted(c["to_instance_id"] for c in env.fed.calls)
    assert targets == ["remote_a", "remote_b"]
    for call in env.fed.calls:
        assert call["event_type"] == FederationEventType.SPACE_ZONE_UPSERTED
        assert call["space_id"] == "sp_test"
        p = call["payload"]
        assert p["zone_id"] == "z_office"
        assert p["name"] == "Office"
        assert p["radius_m"] == 150
        assert p["color"] == "#3b82f6"


async def test_upsert_skips_own_instance(env):
    """The fake repo returns ``self_instance`` in its peer list. The
    outbound must skip it — federating zone events to ourselves would
    be a noisy no-op (we already have the row locally)."""
    await env.bus.publish(_upsert_event())
    assert all(c["to_instance_id"] != "self_instance" for c in env.fed.calls)


async def test_upsert_with_no_remote_members_is_a_noop():
    bus = EventBus()
    spaces = _FakeSpaceRepo(["self_instance"])
    fed = _FakeFederation()
    SpaceZoneOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=spaces,  # type: ignore[arg-type]
    ).wire()
    await bus.publish(_upsert_event())
    assert fed.calls == []


# ─── Delete ─────────────────────────────────────────────────────────────


async def test_delete_fans_with_correct_event_type(env):
    await env.bus.publish(
        SpaceZoneDeleted(
            space_id="sp_test",
            zone_id="z_office",
            deleted_by="u_admin",
        ),
    )
    assert len(env.fed.calls) == 2
    for call in env.fed.calls:
        assert call["event_type"] == FederationEventType.SPACE_ZONE_DELETED
        assert call["payload"]["zone_id"] == "z_office"
        assert call["payload"]["deleted_by"] == "u_admin"


# ─── Defensive paths ────────────────────────────────────────────────────


async def test_send_event_failure_is_swallowed():
    """A failing peer must not break the fan-out for other peers."""
    bus = EventBus()
    spaces = _FakeSpaceRepo(["remote_a", "remote_b"])
    fed = _FakeFederation(raise_on="remote_a")
    SpaceZoneOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=spaces,  # type: ignore[arg-type]
    ).wire()
    await bus.publish(_upsert_event())
    # remote_a raised, but remote_b still got the event.
    assert [c["to_instance_id"] for c in fed.calls] == ["remote_b"]


async def test_list_peers_failure_is_swallowed():
    """If list_member_instances raises, fan-out silently returns rather
    than letting the exception bubble up through the bus."""
    bus = EventBus()

    class _BrokenRepo:
        async def list_member_instances(self, space_id: str) -> list[str]:
            raise RuntimeError("db gone")

    fed = _FakeFederation()
    SpaceZoneOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=_BrokenRepo(),  # type: ignore[arg-type]
    ).wire()
    await bus.publish(_upsert_event())
    assert fed.calls == []


async def test_skips_empty_peer_id():
    """The federation_repo can return rows with empty ``instance_id``
    — defensive code path: skip rather than send to ``""``."""
    bus = EventBus()
    spaces = _FakeSpaceRepo(["", "remote_b"])
    fed = _FakeFederation()
    SpaceZoneOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=spaces,  # type: ignore[arg-type]
    ).wire()
    await bus.publish(_upsert_event())
    assert [c["to_instance_id"] for c in fed.calls] == ["remote_b"]
