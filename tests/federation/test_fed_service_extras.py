"""Coverage extras for FederationService — properties + send_event errors."""

from __future__ import annotations

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.federation_service import FederationService
from socialhome.infrastructure import EventBus, KeyManager
from socialhome.repositories import (
    SqliteFederationRepo,
    SqliteOutboxRepo,
)


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    own_kp = generate_identity_keypair()
    own_iid = derive_instance_id(own_kp.public_key)
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (own_iid, own_kp.private_key.hex(), own_kp.public_key.hex(), "aa" * 32),
    )
    fed_repo = SqliteFederationRepo(db)
    outbox = SqliteOutboxRepo(db)
    bus = EventBus()
    kek = KeyManager.from_data_dir(tmp_dir)
    svc = FederationService(
        db=db,
        federation_repo=fed_repo,
        outbox_repo=outbox,
        key_manager=kek,
        bus=bus,
        own_instance_id=own_iid,
        own_identity_seed=own_kp.private_key,
        own_identity_pk=own_kp.public_key,
    )
    yield svc, fed_repo, kek, own_iid, own_kp
    await db.shutdown()


# ─── Property accessors ──────────────────────────────────────────────────


async def test_own_instance_id_property(env):
    svc, _, _, own_iid, _ = env
    assert svc.own_instance_id == own_iid


async def test_own_identity_seed_property(env):
    svc, _, _, _, own_kp = env
    assert svc.own_identity_seed == own_kp.private_key


# ─── set_ice_servers ────────────────────────────────────────────────────


async def test_set_ice_servers_replaces_value(env):
    svc, _, _, _, _ = env
    svc.set_ice_servers([{"urls": ["stun:x"]}])
    assert svc._ice_servers == [{"urls": ["stun:x"]}]
    svc.set_ice_servers(None)
    assert svc._ice_servers == []


# ─── send_event error paths ──────────────────────────────────────────────


async def test_send_event_unknown_instance(env):
    svc, _, _, _, _ = env
    result = await svc.send_event(
        to_instance_id="never-paired",
        event_type=FederationEventType.PRESENCE_UPDATED,
        payload={},
    )
    assert result.ok is False
    assert result.error == "unknown_instance"


async def test_send_event_failed_session_decrypt(env):
    svc, fed_repo, _, _, _ = env
    peer_kp = generate_identity_keypair()
    # Save with a malformed session-key blob — KEK decrypt will fail.
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote="garbage:bytes",
        key_remote_to_self="garbage:bytes",
        remote_webhook_url="https://x/wh",
        local_webhook_id="wh",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)
    result = await svc.send_event(
        to_instance_id=peer.id,
        event_type=FederationEventType.PRESENCE_UPDATED,
        payload={"x": 1},
    )
    assert result.ok is False
    assert result.error == "key_decrypt_error"


async def test_send_event_transport_error(env):
    """Network failure → ok=False + outbox enqueue."""
    svc, fed_repo, kek, _, _ = env
    peer_kp = generate_identity_keypair()
    session_key = b"\x05" * 32
    wrapped = kek.encrypt(session_key)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_webhook_url="https://nonexistent.invalid/wh",
        local_webhook_id="wh",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)

    class _RaisingClient:
        def post(self, *a, **kw):
            raise ConnectionError("boom")

    svc._http_client = _RaisingClient()
    result = await svc.send_event(
        to_instance_id=peer.id,
        event_type=FederationEventType.PRESENCE_UPDATED,
        payload={},
    )
    assert result.ok is False
    assert result.error == "delivery_failed"


# ─── broadcast_to_peers — empty + with explicit list ────────────────────


async def test_broadcast_to_peers_no_peers(env):
    svc, _, _, _, _ = env
    result = await svc.broadcast_to_peers(
        event_type=FederationEventType.PRESENCE_UPDATED,
        payload={},
    )
    assert result.attempted == 0
    assert result.all_ok is False


async def test_broadcast_to_peers_explicit_list_unknown(env):
    svc, _, _, _, _ = env
    result = await svc.broadcast_to_peers(
        event_type=FederationEventType.PRESENCE_UPDATED,
        payload={},
        instance_ids=["unknown-1", "unknown-2"],
    )
    assert result.attempted == 2
    assert result.failed == 2
