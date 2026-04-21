"""Protocol test: idempotency cache deduplicates inbound events."""

from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from social_home.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    sign_ed25519,
)
from social_home.db.database import AsyncDatabase
from social_home.domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from social_home.federation.federation_service import FederationService
from social_home.infrastructure import EventBus, IdempotencyCache, KeyManager
from social_home.repositories import (
    SqliteFederationRepo,
    SqliteOutboxRepo,
)


pytestmark = pytest.mark.security


def _build_inbound(
    *,
    own_iid: str,
    own_pk: bytes,
    peer_kp,
    session_key: bytes,
    payload: dict,
    msg_id: str = "msg-uniq-1",
    event_type: FederationEventType = FederationEventType.PRESENCE_UPDATED,
) -> bytes:
    """Construct a properly-signed + encrypted federation envelope."""
    import os
    from datetime import datetime, timezone

    payload_json = json.dumps(payload, separators=(",", ":"))
    aead = AESGCM(session_key)
    nonce = os.urandom(12)
    ct = aead.encrypt(nonce, payload_json.encode("utf-8"), None)
    encrypted = b64url_encode(nonce) + ":" + b64url_encode(ct)

    envelope = {
        "msg_id": msg_id,
        "event_type": event_type.value,
        "from_instance": derive_instance_id(peer_kp.public_key),
        "to_instance": own_iid,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "encrypted_payload": encrypted,
        "space_id": None,
        "proto_version": 1,
        "sig_suite": "ed25519",
    }
    sig = sign_ed25519(
        peer_kp.private_key,
        json.dumps(envelope, separators=(",", ":")).encode("utf-8"),
    )
    envelope["signatures"] = {"ed25519": b64url_encode(sig)}
    return json.dumps(envelope).encode("utf-8")


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

    peer_kp = generate_identity_keypair()
    session_key = b"\x42" * 32
    wrapped = kek.encrypt(session_key)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_webhook_url="https://x/wh",
        local_webhook_id="wh-peer",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)

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
    cache = IdempotencyCache(ttl_seconds=60)
    svc.attach_idempotency_cache(cache)

    yield {
        "svc": svc,
        "own_iid": own_iid,
        "own_pk": own_kp.public_key,
        "peer_kp": peer_kp,
        "session_key": session_key,
        "peer": peer,
        "cache": cache,
    }
    await db.shutdown()


# ─── Idempotency dedup ───────────────────────────────────────────────────


async def test_inbound_idempotency_drops_duplicates(env):
    """Same idempotency_key sent twice → second call returns deduped flag."""
    body = _build_inbound(
        own_iid=env["own_iid"],
        own_pk=env["own_pk"],
        peer_kp=env["peer_kp"],
        session_key=env["session_key"],
        payload={"idempotency_key": "ik-1", "user_id": "alice"},
        msg_id="msg-1",
    )
    out = await env["svc"].handle_inbound_webhook(env["peer"].local_webhook_id, body)
    assert out["status"] == "ok"

    body2 = _build_inbound(
        own_iid=env["own_iid"],
        own_pk=env["own_pk"],
        peer_kp=env["peer_kp"],
        session_key=env["session_key"],
        payload={"idempotency_key": "ik-1", "user_id": "alice"},
        msg_id="msg-2",  # distinct msg_id so replay cache doesn't trip
    )
    out2 = await env["svc"].handle_inbound_webhook(env["peer"].local_webhook_id, body2)
    assert out2.get("deduped") is True


async def test_inbound_no_idempotency_key_processes_normally(env):
    body = _build_inbound(
        own_iid=env["own_iid"],
        own_pk=env["own_pk"],
        peer_kp=env["peer_kp"],
        session_key=env["session_key"],
        payload={"user_id": "alice"},
        msg_id="msg-3",
    )
    out = await env["svc"].handle_inbound_webhook(env["peer"].local_webhook_id, body)
    assert out["status"] == "ok"
    assert "deduped" not in out


async def test_inbound_distinct_idempotency_keys_both_processed(env):
    for ik, mid in [("ik-A", "m-A"), ("ik-B", "m-B")]:
        body = _build_inbound(
            own_iid=env["own_iid"],
            own_pk=env["own_pk"],
            peer_kp=env["peer_kp"],
            session_key=env["session_key"],
            payload={"idempotency_key": ik, "user_id": "alice"},
            msg_id=mid,
        )
        out = await env["svc"].handle_inbound_webhook(
            env["peer"].local_webhook_id, body
        )
        assert out["status"] == "ok"
        assert "deduped" not in out
