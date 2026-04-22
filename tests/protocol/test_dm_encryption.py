"""Protocol-level test: DM federation envelopes encrypt content (§25.8.21).

Per the encryption-first rule, DM messages travelling between instances
(``DM_MESSAGE``, ``DM_RELAY``, ``DM_HISTORY_CHUNK``) must travel inside
``encrypted_payload`` — never as a plaintext field on the envelope.

Local-DB storage of the same content is intentionally plaintext (Pascal,
2026-04-15: E2E is the *transport* layer; the host already sees the data
once it lands locally).
"""

from __future__ import annotations

import json

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


pytestmark = pytest.mark.security


# ─── Test environment ────────────────────────────────────────────────────


class _CapturingClient:
    """aiohttp-style fake that records POST bodies instead of sending."""

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
async def fed(tmp_dir):
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

    # Set up a paired peer.
    peer_kp = generate_identity_keypair()
    session_key = b"\x01" * 32
    wrapped = kek.encrypt(session_key)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_webhook_url="https://peer.invalid/wh",
        local_webhook_id="wh-peer",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)

    capture = _CapturingClient()
    svc._http_client = capture
    yield svc, peer, capture
    await db.shutdown()


# ─── DM_MESSAGE encryption ──────────────────────────────────────────────


async def test_dm_message_envelope_has_no_plaintext_content(fed):
    svc, peer, capture = fed
    sensitive_text = "hello pascal — pin code is 4242"
    await svc.send_event(
        to_instance_id=peer.id,
        event_type=FederationEventType.DM_MESSAGE,
        payload={
            "conversation_id": "conv-1",
            "message_id": "msg-1",
            "sender": "alice",
            "content": sensitive_text,
        },
    )
    assert len(capture.bodies) == 1
    envelope = capture.bodies[0]
    raw = json.dumps(envelope)
    # The original plaintext must not appear anywhere in the wire format.
    assert sensitive_text not in raw
    assert "content" not in envelope
    assert "encrypted_payload" in envelope


async def test_dm_relay_envelope_encrypts_content(fed):
    svc, peer, capture = fed
    body_text = "plaintext-must-not-leak"
    await svc.send_event(
        to_instance_id=peer.id,
        event_type=FederationEventType.DM_RELAY,
        payload={
            "conversation_id": "conv-1",
            "messages": [{"id": "m1", "content": body_text}],
        },
    )
    envelope = capture.bodies[0]
    assert body_text not in json.dumps(envelope)


async def test_dm_history_chunk_encrypts_content(fed):
    svc, peer, capture = fed
    secret = "ancient-history-secret"
    await svc.send_event(
        to_instance_id=peer.id,
        event_type=FederationEventType.DM_HISTORY_CHUNK,
        payload={
            "conversation_id": "conv-1",
            "messages": [{"id": "m1", "content": secret}],
        },
    )
    envelope = capture.bodies[0]
    assert secret not in json.dumps(envelope)


async def test_dm_typing_indicator_encrypts_user_id(fed):
    """Even ephemeral metadata like typing indicators must be encrypted —
    the participant set is sensitive (who's talking to whom)."""
    svc, peer, capture = fed
    await svc.send_event(
        to_instance_id=peer.id,
        event_type=FederationEventType.DM_USER_TYPING,
        payload={"conversation_id": "conv-1", "user_id": "alice"},
    )
    envelope = capture.bodies[0]
    assert "alice" not in json.dumps(envelope)


async def test_dm_envelope_routing_fields_are_plaintext(fed):
    """Encryption-first does NOT mean opaque — routing fields must be
    visible so the peer can dispatch."""
    svc, peer, capture = fed
    await svc.send_event(
        to_instance_id=peer.id,
        event_type=FederationEventType.DM_MESSAGE,
        payload={"conversation_id": "c1", "content": "x"},
    )
    envelope = capture.bodies[0]
    assert envelope["event_type"] == "dm_message"
    assert envelope["from_instance"] == svc.own_instance_id
    assert envelope["to_instance"] == peer.id
    assert "msg_id" in envelope
    assert "timestamp" in envelope
    assert "signatures" in envelope and "ed25519" in envelope["signatures"]
