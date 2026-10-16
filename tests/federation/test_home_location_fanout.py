"""Tests for LocalHomeLocationUpdated → LOCAL_HOME_LOCATION_CHANGED fan-out.

Uses InMemoryFederationRepo and InMemoryOutboxRepo from test_federation_service.py.
test_federation_service.py still carries its own private copies pre-dating this
refactor — a future cleanup should consolidate.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.domain.events import LocalHomeLocationUpdated
from socialhome.domain.federation import (
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


def _peer(
    instance_id: str, *, proto_version: int, share_home: bool = True
) -> RemoteInstance:
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
        share_home=share_home,
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


# ─── Tests ───────────────────────────────────────────────────────────────────


_SEND_EVENT_PATH = (
    "socialhome.federation.federation_service.FederationService.send_event"
)


@pytest.mark.asyncio
async def test_local_home_location_fans_out_to_v5_confirmed_peer():
    """LocalHomeLocationUpdated → LOCAL_HOME_LOCATION_CHANGED sent to a
    confirmed peer at proto_version=5."""
    _svc, fed_repo, bus = _make_service()
    await fed_repo.save_instance(_peer("peer-v5", proto_version=5))

    with patch(_SEND_EVENT_PATH, new_callable=AsyncMock) as mock_send:
        await bus.publish(LocalHomeLocationUpdated(latitude=52.52, longitude=13.40))

        mock_send.assert_awaited_once()
        call_kwargs = mock_send.await_args.kwargs
        assert call_kwargs["to_instance_id"] == "peer-v5"
        assert (
            call_kwargs["event_type"] == FederationEventType.LOCAL_HOME_LOCATION_CHANGED
        )
        assert call_kwargs["payload"] == {"latitude": 52.52, "longitude": 13.40}


@pytest.mark.asyncio
async def test_local_home_location_skips_sub_v5_peer():
    """A confirmed peer at proto_version=4 is silently skipped."""
    _svc, fed_repo, bus = _make_service()
    await fed_repo.save_instance(_peer("peer-v4", proto_version=4))

    with patch(_SEND_EVENT_PATH, new_callable=AsyncMock) as mock_send:
        await bus.publish(LocalHomeLocationUpdated(latitude=52.52, longitude=13.40))

        mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_home_location_skips_v4_sends_to_v5():
    """When both a v4 and a v5 peer are present, only v5 receives."""
    _svc, fed_repo, bus = _make_service()
    await fed_repo.save_instance(_peer("peer-v4", proto_version=4))
    await fed_repo.save_instance(_peer("peer-v5", proto_version=5))

    with patch(_SEND_EVENT_PATH, new_callable=AsyncMock) as mock_send:
        await bus.publish(LocalHomeLocationUpdated(latitude=10.0, longitude=20.0))

        assert mock_send.await_count == 1
        call_kwargs = mock_send.await_args.kwargs
        assert call_kwargs["to_instance_id"] == "peer-v5"


@pytest.mark.asyncio
async def test_local_home_location_skips_share_home_false_peer():
    """A v5 confirmed peer with share_home=False is silently skipped."""
    _svc, fed_repo, bus = _make_service()
    await fed_repo.save_instance(
        _peer("peer-v5-no-share", proto_version=5, share_home=False)
    )

    with patch(_SEND_EVENT_PATH, new_callable=AsyncMock) as mock_send:
        await bus.publish(LocalHomeLocationUpdated(latitude=52.52, longitude=13.40))

        mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_home_location_skips_when_all_peers_have_share_home_false():
    """All confirmed peers with share_home=False → zero sends, no error."""
    _svc, fed_repo, bus = _make_service()
    await fed_repo.save_instance(_peer("peer-v5-a", proto_version=5, share_home=False))
    await fed_repo.save_instance(_peer("peer-v5-b", proto_version=5, share_home=False))

    with patch(_SEND_EVENT_PATH, new_callable=AsyncMock) as mock_send:
        await bus.publish(LocalHomeLocationUpdated(latitude=52.52, longitude=13.40))

        mock_send.assert_not_awaited()
