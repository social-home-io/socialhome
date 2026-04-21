"""HTTP tests for /webhook/{id} — verify the inbound pipeline runs."""

from __future__ import annotations

import json
import os

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from social_home.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    sign_ed25519,
)
from social_home.domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)


def _build_envelope(
    *,
    own_iid: str,
    peer_kp,
    session_key: bytes,
    payload: dict,
    msg_id: str = "msg-1",
    event_type: FederationEventType = FederationEventType.PRESENCE_UPDATED,
    timestamp: str | None = None,
) -> bytes:
    from datetime import datetime, timezone

    aead = AESGCM(session_key)
    nonce = os.urandom(12)
    pj = json.dumps(payload, separators=(",", ":"))
    ct = aead.encrypt(nonce, pj.encode("utf-8"), None)
    encrypted = b64url_encode(nonce) + ":" + b64url_encode(ct)

    envelope: dict = {
        "msg_id": msg_id,
        "event_type": event_type.value,
        "from_instance": derive_instance_id(peer_kp.public_key),
        "to_instance": own_iid,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
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
async def env(client):
    """Add a paired peer to the route-conftest client + return the keys."""
    from social_home.app_keys import (
        federation_repo_key,
        federation_service_key,
        key_manager_key,
    )

    db = client._db
    iid_row = await db.fetchone(
        "SELECT instance_id FROM instance_identity WHERE id='self'",
    )
    own_iid = iid_row["instance_id"]
    fed_repo = client.server.app[federation_repo_key]
    kek = client.server.app[key_manager_key]
    fed_svc = client.server.app[federation_service_key]

    peer_kp = generate_identity_keypair()
    session_key = b"\x07" * 32
    wrapped = kek.encrypt(session_key)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_webhook_url="https://x/wh",
        local_webhook_id="wh-test",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)
    return {
        "own_iid": own_iid,
        "peer_kp": peer_kp,
        "session_key": session_key,
        "peer": peer,
        "fed_svc": fed_svc,
    }


# ─── Happy path ──────────────────────────────────────────────────────────


async def test_inbound_valid_envelope_returns_ok(client, env):
    body = _build_envelope(
        own_iid=env["own_iid"],
        peer_kp=env["peer_kp"],
        session_key=env["session_key"],
        payload={"user_id": "alice", "state": "home"},
    )
    r = await client.post("/webhook/wh-test", data=body)
    assert r.status == 200
    assert (await r.json())["status"] == "ok"


# ─── Validation rejections ──────────────────────────────────────────────


async def test_inbound_unknown_webhook_404(client, env):
    body = _build_envelope(
        own_iid=env["own_iid"],
        peer_kp=env["peer_kp"],
        session_key=env["session_key"],
        payload={},
    )
    r = await client.post("/webhook/nonexistent", data=body)
    assert r.status == 404


async def test_inbound_invalid_json_400(client):
    r = await client.post("/webhook/wh-test", data=b"not json")
    assert r.status == 400


async def test_inbound_missing_fields_400(client):
    r = await client.post("/webhook/wh-test", data=b'{"msg_id":"x"}')
    assert r.status == 400


async def test_inbound_oversized_envelope_rejected(client):
    """aiohttp may return 400 (its own client-max-size guard) or 413
    (our route's explicit 1 MiB check) — both indicate proper rejection."""
    big = b"x" * (2 * 1024 * 1024)
    r = await client.post("/webhook/wh-test", data=big)
    assert r.status in (400, 413)


async def test_inbound_unknown_event_type_400(client, env):
    body_dict = {
        "msg_id": "x",
        "event_type": "totally_made_up_event",
        "from_instance": "a",
        "to_instance": "b",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "encrypted_payload": "x:y",
        "sig_suite": "ed25519",
        "signatures": {"ed25519": "z"},
    }
    r = await client.post(
        "/webhook/wh-test",
        data=json.dumps(body_dict).encode(),
    )
    assert r.status == 400


async def test_inbound_old_timestamp_410(client, env):
    """Timestamp >5min skew → 410 (gone)."""
    body = _build_envelope(
        own_iid=env["own_iid"],
        peer_kp=env["peer_kp"],
        session_key=env["session_key"],
        payload={},
        timestamp="2020-01-01T00:00:00+00:00",
    )
    r = await client.post("/webhook/wh-test", data=body)
    assert r.status == 410


async def test_inbound_bad_signature_403(client, env):
    """Tampering the envelope → 403."""
    body = _build_envelope(
        own_iid=env["own_iid"],
        peer_kp=env["peer_kp"],
        session_key=env["session_key"],
        payload={},
    )
    # Mutate one byte of the encrypted_payload → signature mismatch.
    obj = json.loads(body)
    obj["encrypted_payload"] = "AA" + obj["encrypted_payload"][2:]
    r = await client.post(
        "/webhook/wh-test",
        data=json.dumps(obj).encode(),
    )
    assert r.status == 403


async def test_inbound_replay_410(client, env):
    """Same msg_id twice → second returns 410 (gone)."""
    body = _build_envelope(
        own_iid=env["own_iid"],
        peer_kp=env["peer_kp"],
        session_key=env["session_key"],
        payload={"x": 1},
        msg_id="dup-msg-1",
    )
    r1 = await client.post("/webhook/wh-test", data=body)
    assert r1.status == 200
    r2 = await client.post("/webhook/wh-test", data=body)
    assert r2.status == 410


# ─── Public path / no auth required ─────────────────────────────────────


async def test_inbound_does_not_require_bearer_token(client, env):
    """The webhook is in _DEFAULT_PUBLIC_PATHS — no Authorization needed."""
    body = _build_envelope(
        own_iid=env["own_iid"],
        peer_kp=env["peer_kp"],
        session_key=env["session_key"],
        payload={"user_id": "alice"},
        msg_id="no-auth-msg",
    )
    # No headers — auth middleware must let this through.
    r = await client.post("/webhook/wh-test", data=body)
    assert r.status == 200
