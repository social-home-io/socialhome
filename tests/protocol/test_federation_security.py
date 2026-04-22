"""Protocol-level security tests for the federation pipeline (§27, §25).

Marked ``@pytest.mark.security`` — these are spec-mandated **release
blockers**.  Per CLAUDE.md they must run before every commit touching
federation or presence code.

Coverage:

* §24.11 inbound validation pipeline — every step rejects invalid input.
* §25.6.2 audit fixes — S-2 / S-7 / S-13 / S-14 / S-15 / S-16 / S-17.
* §25.8.21 encryption-first rule — no plaintext payload field on the
  wire.
"""

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
from socialhome.federation.sync_manager import (
    SyncSessionManager,
    new_sync_id,
)
from socialhome.federation.sync_rtc import SyncRtcSession
from socialhome.infrastructure import EventBus, KeyManager
from socialhome.repositories import (
    SqliteFederationRepo,
    SqliteOutboxRepo,
)
from socialhome.federation.sdp_signing import (
    sign_rtc_offer,
    verify_rtc_offer,
)

pytestmark = pytest.mark.security


# ─── Test environment ────────────────────────────────────────────────────


@pytest.fixture
async def fed(tmp_dir):
    """A FederationService wired against a real SQLite DB + KeyManager."""
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
    key_mgr = KeyManager.from_data_dir(tmp_dir)

    svc = FederationService(
        db=db,
        federation_repo=fed_repo,
        outbox_repo=outbox,
        key_manager=key_mgr,
        bus=bus,
        own_instance_id=own_iid,
        own_identity_seed=own_kp.private_key,
        own_identity_pk=own_kp.public_key,
    )
    await svc.warm_replay_cache()
    yield {
        "svc": svc,
        "fed_repo": fed_repo,
        "db": db,
        "key_mgr": key_mgr,
        "own_iid": own_iid,
        "own_kp": own_kp,
    }
    await db.shutdown()


# ─── §24.11 inbound pipeline ─────────────────────────────────────────────


async def test_inbound_rejects_unparseable_json(fed):
    with pytest.raises(ValueError):
        await fed["svc"].handle_inbound_webhook("wh-1", b"not json")


async def test_inbound_rejects_missing_required_fields(fed):
    body = b'{"msg_id":"x"}'
    with pytest.raises(ValueError, match="Missing required fields"):
        await fed["svc"].handle_inbound_webhook("wh-1", body)


async def test_inbound_rejects_unknown_event_type(fed):
    body = (
        b'{"msg_id":"x","event_type":"not_a_real_event",'
        b'"from_instance":"a","to_instance":"b","timestamp":"2026-01-01T00:00:00+00:00",'
        b'"encrypted_payload":"x:y","sig_suite":"ed25519",'
        b'"signatures":{"ed25519":"z"}}'
    )
    with pytest.raises(ValueError, match="Unknown event_type"):
        await fed["svc"].handle_inbound_webhook("wh-1", body)


async def test_inbound_rejects_unknown_webhook_id(fed):
    body = (
        b'{"msg_id":"x","event_type":"presence_updated",'
        b'"from_instance":"a","to_instance":"b","timestamp":"2026-01-01T00:00:00+00:00",'
        b'"encrypted_payload":"x:y","sig_suite":"ed25519",'
        b'"signatures":{"ed25519":"z"}}'
    )
    with pytest.raises(ValueError, match="No instance found"):
        await fed["svc"].handle_inbound_webhook("nonexistent-wh", body)


# ─── §25.6.2 audit fixes ─────────────────────────────────────────────────


async def test_s2_sync_id_is_high_entropy():
    """S-2: 128-bit URL-safe token — no collisions in 1000 samples."""
    samples = {new_sync_id() for _ in range(1000)}
    assert len(samples) == 1000


def test_s7_ice_candidate_validation_rejects_oversize():
    """S-7: ICE candidate over 2 KB is dropped."""
    huge = "candidate:" + ("x" * 2050)
    assert SyncSessionManager.validate_ice_candidate(huge) is False


def test_s7_ice_candidate_validation_rejects_bad_prefix():
    """S-7: anything not starting with ``candidate:`` is dropped."""
    assert SyncSessionManager.validate_ice_candidate("bogus 1 2 3") is False


async def test_s13_create_answer_is_distinct_from_set_answer():
    """S-13: requester's create_answer is NOT the provider's set_answer."""
    _requester = SyncRtcSession(
        sync_id="x",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        role="requester",
    )
    provider = SyncRtcSession(
        sync_id="y",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        role="provider",
    )
    with pytest.raises(RuntimeError):
        # Provider must NOT have create_answer available.
        await provider.create_answer("x")


async def test_s14_answer_origin_guard():
    """S-14: SPACE_SYNC_ANSWER from the wrong instance is rejected."""

    class _R:
        async def get_instance(self, iid):
            return None

    mgr = SyncSessionManager(_R())
    await mgr.begin_session(
        sync_id="s1",
        space_id="sp1",
        requester_instance_id="alice",
        provider_instance_id="me",
    )
    ok = await mgr.apply_answer(
        sync_id="s1",
        sdp_answer="v=0\r\n",
        from_instance="hostile",
    )
    assert ok is False


async def test_s15_relay_fallback_uses_new_sync_begin():
    """S-15: trigger_relay_sync emits SPACE_SYNC_BEGIN with prefer_direct=False."""

    class _R:
        async def get_instance(self, iid):
            return None

    mgr = SyncSessionManager(_R())
    await mgr.begin_session(
        sync_id="s1",
        space_id="sp1",
        requester_instance_id="alice",
        provider_instance_id="me",
        sync_mode="initial",
    )
    decision = await mgr.trigger_relay_sync("s1")
    assert decision.next_event == FederationEventType.SPACE_SYNC_BEGIN
    assert decision.next_payload["prefer_direct"] is False


async def test_s16_sync_mode_is_a_formal_field():
    """S-16: sync_mode must be a real constructor field, not a getattr."""
    s = SyncRtcSession(
        sync_id="x",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        sync_mode="full",
    )
    assert s.sync_mode == "full"


async def test_s16_invalid_sync_mode_rejected():
    with pytest.raises(ValueError):
        SyncRtcSession(
            sync_id="x",
            space_id="sp1",
            requester_instance_id="r",
            provider_instance_id="p",
            sync_mode="ultra-secret-mode",
        )


async def test_s17_instance_sync_status_unknown_sender_silently_ignored():
    """S-17: spaces from an unknown peer are dropped — no abuse vector."""

    class _R:
        async def get_instance(self, iid):
            return None

    mgr = SyncSessionManager(_R())
    spaces = await mgr.validate_instance_sync_status(
        from_instance="unknown",
        payload={"spaces": ["sp1", "sp2"]},
    )
    assert spaces == []


async def test_s17_instance_sync_status_caps_payload_size():
    """S-17: ≤ 100 spaces per payload — reject larger."""

    class _R:
        async def get_instance(self, iid):
            return RemoteInstance(
                id="alice",
                display_name="alice",
                remote_identity_pk="aa" * 32,
                key_self_to_remote="x",
                key_remote_to_self="x",
                remote_webhook_url="https://x",
                local_webhook_id="wh",
                status=PairingStatus.CONFIRMED,
                source=InstanceSource.MANUAL,
            )

    mgr = SyncSessionManager(_R())
    spaces = await mgr.validate_instance_sync_status(
        from_instance="alice",
        payload={"spaces": [f"sp-{i}" for i in range(101)]},
    )
    assert spaces == []


# ─── §25.8.21 encryption-first ───────────────────────────────────────────


async def test_outbound_envelope_has_no_plaintext_payload(fed):
    """Encryption-first: send_event must NEVER include a plaintext payload."""
    svc, fed_repo, key_mgr, own_iid = (
        fed["svc"],
        fed["fed_repo"],
        fed["key_mgr"],
        fed["own_iid"],
    )
    # Set up a paired peer.
    peer_kp = generate_identity_keypair()
    session_key = b"\x01" * 32
    wrapped = key_mgr.encrypt(session_key)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_webhook_url="https://nonexistent.invalid/wh",
        local_webhook_id="wh-peer",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)

    sent_bodies: list[dict] = []

    class _Resp:
        def __init__(self):
            self.status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Capture:
        def post(self, url, *, json=None, **kw):
            sent_bodies.append(json)
            return _Resp()

    svc._http_client = _Capture()
    await svc.send_event(
        to_instance_id=peer.id,
        event_type=FederationEventType.PRESENCE_UPDATED,
        payload={"user_id": "alice", "state": "home"},
    )
    assert len(sent_bodies) == 1
    envelope = sent_bodies[0]
    # Plaintext routing only — no "payload", no "user_id".
    assert "payload" not in envelope
    assert "user_id" not in envelope
    assert "encrypted_payload" in envelope
    # Routing fields present.
    assert envelope["event_type"] == "presence_updated"
    assert envelope["from_instance"] == own_iid
    assert envelope["to_instance"] == peer.id


# ─── federation.sdp_signing ─────────────────────────────────────────────


def test_signed_sdp_roundtrip():
    """Identity-signed SDPs verify against the same key."""
    kp = generate_identity_keypair()
    signed = sign_rtc_offer("v=0\r\nofr\r\n", "offer", identity_seed=kp.private_key)
    assert verify_rtc_offer(signed, remote_public_key=kp.public_key) is True


def test_signed_sdp_rejects_tampered_sdp():
    """Modifying the SDP invalidates the signature — defends against MITM."""
    kp = generate_identity_keypair()
    signed = sign_rtc_offer("v=0\r\norig\r\n", "offer", identity_seed=kp.private_key)
    tampered = type(signed)(
        sdp="v=0\r\nMITM\r\n",
        sdp_type=signed.sdp_type,
        signature=signed.signature,
    )
    assert verify_rtc_offer(tampered, remote_public_key=kp.public_key) is False


def test_signed_sdp_rejects_wrong_key():
    kp_a = generate_identity_keypair()
    kp_b = generate_identity_keypair()
    signed = sign_rtc_offer("v=0\r\n", "offer", identity_seed=kp_a.private_key)
    assert verify_rtc_offer(signed, remote_public_key=kp_b.public_key) is False
