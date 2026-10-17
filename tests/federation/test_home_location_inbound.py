"""Tests for inbound LOCAL_HOME_LOCATION_CHANGED → RemoteInstance.home_lat/lon update + PeerHomeChanged."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.domain.events import PeerHomeChanged
from socialhome.domain.federation import (
    FederationEvent,
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.federation_service import FederationService
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from tests.federation.test_federation_service import (
    InMemoryFederationRepo,
    InMemoryOutboxRepo,
)

# ─── Shared helpers ──────────────────────────────────────────────────────────


def _make_kek() -> KeyManager:
    return KeyManager(os.urandom(32))


def _peer(instance_id: str, *, proto_version: int = 5) -> RemoteInstance:
    return RemoteInstance(
        id=instance_id,
        display_name=instance_id,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url="https://example.test/inbox",
        local_inbox_id="local-" + instance_id,
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
        proto_version=proto_version,
    )


def _make_service() -> tuple[FederationService, InMemoryFederationRepo, EventBus]:
    own_kp = generate_identity_keypair()
    own_id = derive_instance_id(own_kp.public_key)
    fed_repo = InMemoryFederationRepo()
    bus = EventBus()
    svc = FederationService(
        db=MagicMock(),
        federation_repo=fed_repo,
        outbox_repo=InMemoryOutboxRepo(),
        key_manager=_make_kek(),
        bus=bus,
        own_instance_id=own_id,
        own_identity_seed=own_kp.private_key,
        own_identity_pk=own_kp.public_key,
    )
    return svc, fed_repo, bus


def _event(
    from_instance: str,
    payload: dict,
) -> FederationEvent:
    return FederationEvent(
        msg_id="test-msg-id",
        event_type=FederationEventType.LOCAL_HOME_LOCATION_CHANGED,
        from_instance=from_instance,
        to_instance="self",
        timestamp="2026-01-01T00:00:00+00:00",
        payload=payload,
    )


# ─── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inbound_home_location_updates_row_and_publishes_event():
    """Valid payload: RemoteInstance.home_lat/lon updated + PeerHomeChanged published."""
    svc, fed_repo, bus = _make_service()
    await fed_repo.save_instance(_peer("peer-1"))

    received: list[PeerHomeChanged] = []
    bus.subscribe(PeerHomeChanged, received.append)

    await svc._on_local_home_location_changed(
        _event("peer-1", {"latitude": 52.5200, "longitude": 13.4050})
    )

    # Row updated
    inst = await fed_repo.get_instance("peer-1")
    assert inst is not None
    assert inst.home_lat == 52.52
    assert inst.home_lon == 13.405

    # Event published
    assert len(received) == 1
    ev = received[0]
    assert ev.instance_id == "peer-1"
    assert ev.latitude == 52.52
    assert ev.longitude == 13.405


@pytest.mark.asyncio
async def test_inbound_home_location_truncates_to_4dp():
    """Inbound coordinates are truncated to 4dp (belt-and-braces, §25)."""
    svc, fed_repo, bus = _make_service()
    await fed_repo.save_instance(_peer("peer-2"))

    received: list[PeerHomeChanged] = []
    bus.subscribe(PeerHomeChanged, received.append)

    await svc._on_local_home_location_changed(
        _event("peer-2", {"latitude": 52.520099999, "longitude": 13.405099999})
    )

    inst = await fed_repo.get_instance("peer-2")
    assert inst is not None
    assert inst.home_lat == round(52.520099999, 4)
    assert inst.home_lon == round(13.405099999, 4)
    assert len(received) == 1
    ev = received[0]
    assert ev.latitude == round(52.520099999, 4)
    assert ev.longitude == round(13.405099999, 4)


@pytest.mark.asyncio
async def test_inbound_home_location_missing_fields_is_silent_noop():
    """Missing latitude/longitude → no DB update, no event, no exception."""
    svc, fed_repo, bus = _make_service()
    await fed_repo.save_instance(_peer("peer-3"))

    received: list[PeerHomeChanged] = []
    bus.subscribe(PeerHomeChanged, received.append)

    # Empty payload — both fields absent
    await svc._on_local_home_location_changed(_event("peer-3", {}))

    inst = await fed_repo.get_instance("peer-3")
    assert inst is not None
    assert inst.home_lat is None
    assert inst.home_lon is None
    assert received == []


@pytest.mark.asyncio
async def test_inbound_home_location_missing_longitude_is_silent_noop():
    """Missing longitude alone → no DB update, no event, no exception."""
    svc, fed_repo, bus = _make_service()
    await fed_repo.save_instance(_peer("peer-4"))

    received: list[PeerHomeChanged] = []
    bus.subscribe(PeerHomeChanged, received.append)

    await svc._on_local_home_location_changed(_event("peer-4", {"latitude": 52.52}))

    inst = await fed_repo.get_instance("peer-4")
    assert inst is not None
    assert inst.home_lat is None
    assert received == []


@pytest.mark.asyncio
async def test_inbound_home_location_non_numeric_is_silent_noop():
    """Non-numeric coordinate strings → no DB update, no event, no exception."""
    svc, fed_repo, bus = _make_service()
    await fed_repo.save_instance(_peer("peer-5"))

    received: list[PeerHomeChanged] = []
    bus.subscribe(PeerHomeChanged, received.append)

    await svc._on_local_home_location_changed(
        _event("peer-5", {"latitude": "not-a-float", "longitude": "also-bad"})
    )

    inst = await fed_repo.get_instance("peer-5")
    assert inst is not None
    assert inst.home_lat is None
    assert received == []


@pytest.mark.asyncio
async def test_inbound_null_coords_clears_peer_row():
    """Both-null payload clears the peer's home_lat/home_lon + publishes PeerHomeChanged(None, None)."""
    svc, fed_repo, bus = _make_service()
    peer = _peer("peer-6")
    # Seed the peer with coords so we can verify they get cleared.
    import dataclasses

    peer_with_coords = dataclasses.replace(peer, home_lat=52.52, home_lon=13.405)
    await fed_repo.save_instance(peer_with_coords)

    received: list[PeerHomeChanged] = []
    bus.subscribe(PeerHomeChanged, received.append)

    await svc._on_local_home_location_changed(
        _event("peer-6", {"latitude": None, "longitude": None})
    )

    inst = await fed_repo.get_instance("peer-6")
    assert inst is not None
    assert inst.home_lat is None
    assert inst.home_lon is None

    assert len(received) == 1
    ev = received[0]
    assert ev.instance_id == "peer-6"
    assert ev.latitude is None
    assert ev.longitude is None


@pytest.mark.asyncio
async def test_inbound_asymmetric_null_is_dropped():
    """Asymmetric nulls (one None, one float) → no DB write, no publish, warning only."""
    svc, fed_repo, bus = _make_service()
    await fed_repo.save_instance(_peer("peer-7"))

    received: list[PeerHomeChanged] = []
    bus.subscribe(PeerHomeChanged, received.append)

    await svc._on_local_home_location_changed(
        _event("peer-7", {"latitude": 52.5, "longitude": None})
    )

    inst = await fed_repo.get_instance("peer-7")
    assert inst is not None
    assert inst.home_lat is None
    assert inst.home_lon is None
    assert received == []
