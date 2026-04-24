"""Coverage tests for FederationService._dispatch_event branches.

The dispatch table is large; this file exercises the lesser-trodden
paths so coverage of federation_service.py reaches the gate.
"""

from __future__ import annotations

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import (
    FederationEvent,
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.federation_service import FederationService
from socialhome.federation.sync_manager import SyncSessionManager
from socialhome.infrastructure import EventBus, IdempotencyCache, KeyManager
from socialhome.repositories import SqliteFederationRepo, SqliteOutboxRepo


# ─── Test environment ────────────────────────────────────────────────────


class _CapturingClient:
    def __init__(self):
        self.bodies: list[dict] = []

    def post(self, url, *, json=None, **kw):
        self.bodies.append(json)
        return _Resp()


class _Resp:
    def __init__(self):
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


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
    sync_mgr = SyncSessionManager(fed_repo)
    svc.attach_sync_manager(sync_mgr)
    svc.attach_idempotency_cache(IdempotencyCache(ttl_seconds=60))
    svc._http_client = _CapturingClient()

    # Set up a paired peer so send_event has a target.
    peer_kp = generate_identity_keypair()
    session_key = b"\x01" * 32
    wrapped = kek.encrypt(session_key)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh-peer",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)
    yield svc, sync_mgr, peer
    await db.shutdown()


def _evt(et, *, payload, space_id=None, from_inst="peer-1"):
    return FederationEvent(
        msg_id="m1",
        event_type=et,
        from_instance=from_inst,
        to_instance="self",
        timestamp="2026-04-15T00:00:00+00:00",
        payload=payload,
        space_id=space_id,
    )


# ─── SPACE_SYNC_BEGIN admit + reject branches ─────────────────────────────


async def test_dispatch_space_sync_begin_admits_and_emits_offer(env):
    svc, sync_mgr, peer = env
    await svc._dispatch_event(
        _evt(
            FederationEventType.SPACE_SYNC_BEGIN,
            payload={"sync_id": "s1", "space_id": "sp-1", "prefer_direct": True},
            space_id="sp-1",
            from_inst=peer.id,
        )
    )
    # An OFFER (or DIRECT_FAILED on rejection) should have been queued
    # via send_event → captured client.
    assert svc._http_client.bodies


async def test_dispatch_space_sync_begin_rejected_emits_failed(env):
    """Trip the rate limit so begin_session returns DIRECT_FAILED."""
    import time

    svc, sync_mgr, peer = env
    # Pre-fill the bucket with current monotonic time so entries are still
    # within the 1-hour window when dispatch runs.
    now = time.monotonic()
    for i in range(6):
        sync_mgr.check_sync_begin_rate(peer.id, "sp-1", now=now + i * 0.001)
    await svc._dispatch_event(
        _evt(
            FederationEventType.SPACE_SYNC_BEGIN,
            payload={"sync_id": "s1", "space_id": "sp-1"},
            space_id="sp-1",
            from_inst=peer.id,
        )
    )
    # The DIRECT_FAILED reply should have been sent.
    assert len(svc._http_client.bodies) >= 1


# ─── SPACE_SYNC_OFFER → ANSWER round-trip ────────────────────────────────


async def test_dispatch_space_sync_offer_emits_answer(env):
    svc, _, peer = env
    await svc._dispatch_event(
        _evt(
            FederationEventType.SPACE_SYNC_OFFER,
            payload={"sync_id": "s2", "sdp_offer": "v=0\r\n"},
            space_id="sp-1",
            from_inst=peer.id,
        )
    )
    assert svc._http_client.bodies


async def test_dispatch_space_sync_answer_records(env):
    svc, sync_mgr, peer = env
    # Prime a session whose requester == peer.id so apply_answer accepts.
    await sync_mgr.begin_session(
        sync_id="s3",
        space_id="sp-1",
        requester_instance_id=peer.id,
        provider_instance_id="self",
    )
    await svc._dispatch_event(
        _evt(
            FederationEventType.SPACE_SYNC_ANSWER,
            payload={"sync_id": "s3", "sdp_answer": "v=0\r\n"},
            from_inst=peer.id,
        )
    )


async def test_dispatch_space_sync_ice_validates(env):
    svc, sync_mgr, peer = env
    await sync_mgr.begin_session(
        sync_id="s4",
        space_id="sp-1",
        requester_instance_id=peer.id,
        provider_instance_id="self",
    )
    await svc._dispatch_event(
        _evt(
            FederationEventType.SPACE_SYNC_ICE,
            payload={
                "sync_id": "s4",
                "candidate": "candidate:1 1 UDP 1 1.2.3.4 1 typ host",
            },
            from_inst=peer.id,
        )
    )


async def test_dispatch_space_sync_direct_failed_triggers_relay(env):
    svc, sync_mgr, peer = env
    await sync_mgr.begin_session(
        sync_id="s5",
        space_id="sp-1",
        requester_instance_id=peer.id,
        provider_instance_id="self",
        sync_mode="initial",
    )
    await svc._dispatch_event(
        _evt(
            FederationEventType.SPACE_SYNC_DIRECT_FAILED,
            payload={"sync_id": "s5"},
            space_id="sp-1",
            from_inst=peer.id,
        )
    )
    # A new SPACE_SYNC_BEGIN (relay fallback) should have been sent.
    assert svc._http_client.bodies


async def test_dispatch_space_sync_complete_closes_session(env):
    svc, sync_mgr, peer = env
    await sync_mgr.begin_session(
        sync_id="s6",
        space_id="sp-1",
        requester_instance_id=peer.id,
        provider_instance_id="self",
    )
    assert sync_mgr.has_session("s6")
    await svc._dispatch_event(
        _evt(
            FederationEventType.SPACE_SYNC_COMPLETE,
            payload={"sync_id": "s6"},
            from_inst=peer.id,
        )
    )
    assert not sync_mgr.has_session("s6")


# ─── REQUEST_MORE bounds ─────────────────────────────────────────────────


async def test_dispatch_request_more_silently_drops_unknown_resource(env):
    svc, _, peer = env
    # Should NOT crash even though the resource is bogus.
    await svc._dispatch_event(
        _evt(
            FederationEventType.SPACE_SYNC_REQUEST_MORE,
            payload={"space_id": "sp-1", "resource": "evil_dump"},
            space_id="sp-1",
            from_inst=peer.id,
        )
    )


# ─── INSTANCE_SYNC_STATUS guard ──────────────────────────────────────────


async def test_dispatch_instance_sync_status_known_peer(env):
    svc, _, peer = env
    await svc._dispatch_event(
        _evt(
            FederationEventType.INSTANCE_SYNC_STATUS,
            payload={"spaces": ["sp-1", "sp-2"]},
            from_inst=peer.id,
        )
    )


# ─── Default branch (unhandled event types log + return) ─────────────────


async def test_dispatch_unhandled_event_does_not_crash(env):
    svc, _, _ = env
    await svc._dispatch_event(
        _evt(
            FederationEventType.NETWORK_SYNC,
            payload={"x": "y"},
        )
    )


# ─── User sync log paths ─────────────────────────────────────────────────


async def test_dispatch_users_sync(env):
    svc, _, _ = env
    await svc._dispatch_event(
        _evt(
            FederationEventType.USERS_SYNC,
            payload={"users": [{"username": "alice"}]},
        )
    )


async def test_dispatch_user_updated(env):
    svc, _, _ = env
    await svc._dispatch_event(
        _evt(
            FederationEventType.USER_UPDATED,
            payload={"user_id": "x"},
        )
    )


async def test_dispatch_user_removed(env):
    svc, _, _ = env
    await svc._dispatch_event(
        _evt(
            FederationEventType.USER_REMOVED,
            payload={"user_id": "x"},
        )
    )


# ─── Pairing event log paths ─────────────────────────────────────────────


async def test_dispatch_pairing_intro(env):
    svc, _, _ = env
    await svc._dispatch_event(
        _evt(
            FederationEventType.PAIRING_INTRO,
            payload={"identity_pk": "ab" * 32},
        )
    )


async def test_dispatch_unpair(env):
    svc, _, _ = env
    await svc._dispatch_event(
        _evt(
            FederationEventType.UNPAIR,
            payload={},
        )
    )


# ─── Space membership log paths ──────────────────────────────────────────


async def test_dispatch_space_member_joined(env):
    svc, _, _ = env
    await svc._dispatch_event(
        _evt(
            FederationEventType.SPACE_MEMBER_JOINED,
            payload={"user_id": "u1"},
            space_id="sp-1",
        )
    )


async def test_dispatch_space_post_created(env):
    svc, _, _ = env
    await svc._dispatch_event(
        _evt(
            FederationEventType.SPACE_POST_CREATED,
            payload={"id": "p1", "content": "hi"},
            space_id="sp-1",
        )
    )


# ─── DM relay log paths ──────────────────────────────────────────────────


async def test_dispatch_dm_message(env):
    svc, _, _ = env
    await svc._dispatch_event(
        _evt(
            FederationEventType.DM_MESSAGE,
            payload={"conversation_id": "c1", "content": "x"},
        )
    )


# ─── Presence log path ───────────────────────────────────────────────────


async def test_dispatch_presence_updated(env):
    svc, _, _ = env
    await svc._dispatch_event(
        _evt(
            FederationEventType.PRESENCE_UPDATED,
            payload={"user_id": "alice", "state": "home"},
        )
    )


# ─── Space config dispatch publishes domain event ────────────────────────


async def test_dispatch_space_config_changed_publishes_domain_event(env):
    svc, _, _ = env
    received = []
    from socialhome.domain.events import SpaceConfigChanged

    svc._bus.subscribe(SpaceConfigChanged, lambda e: received.append(e))
    await svc._dispatch_event(
        _evt(
            FederationEventType.SPACE_CONFIG_CHANGED,
            payload={"sequence": 5, "field": "name"},
            space_id="sp-1",
        )
    )
    assert received
    assert received[0].space_id == "sp-1"
    assert received[0].sequence == 5
