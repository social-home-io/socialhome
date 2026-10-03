"""HTTP tests for /federation/inbox/{id} — verify the inbound pipeline runs."""

from __future__ import annotations

import json
import os

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    sign_ed25519,
)
from socialhome.domain.federation import (
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
    from socialhome.app_keys import (
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
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh-test",
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
    r = await client.post("/federation/inbox/wh-test", data=body)
    assert r.status == 200
    assert (await r.json())["status"] == "ok"


# ─── Validation rejections ──────────────────────────────────────────────


async def test_inbound_unknown_inbox_404(client, env):
    body = _build_envelope(
        own_iid=env["own_iid"],
        peer_kp=env["peer_kp"],
        session_key=env["session_key"],
        payload={},
    )
    r = await client.post("/federation/inbox/nonexistent", data=body)
    assert r.status == 404


async def test_inbound_invalid_json_400(client):
    r = await client.post("/federation/inbox/wh-test", data=b"not json")
    assert r.status == 400


async def test_inbound_missing_fields_400(client):
    r = await client.post("/federation/inbox/wh-test", data=b'{"msg_id":"x"}')
    assert r.status == 400


async def test_inbound_oversized_envelope_rejected(client):
    """aiohttp may return 400 (its own client-max-size guard) or 413
    (our route's explicit 1 MiB check) — both indicate proper rejection."""
    big = b"x" * (2 * 1024 * 1024)
    r = await client.post("/federation/inbox/wh-test", data=big)
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
        "/federation/inbox/wh-test",
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
    r = await client.post("/federation/inbox/wh-test", data=body)
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
        "/federation/inbox/wh-test",
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
    r1 = await client.post("/federation/inbox/wh-test", data=body)
    assert r1.status == 200
    r2 = await client.post("/federation/inbox/wh-test", data=body)
    assert r2.status == 410


async def test_inbound_replay_logs_info_for_ephemeral_event(client, env, caplog):
    """A replayed ephemeral event (capabilities / presence / RTC
    signaling / …) is the DataChannel → HTTPS-inbox failover doing its
    job — log it at INFO so the noise doesn't crowd out real WARNING
    signals. Content events stay WARNING; see the test below."""
    import logging as _logging

    body = _build_envelope(
        own_iid=env["own_iid"],
        peer_kp=env["peer_kp"],
        session_key=env["session_key"],
        payload={"x": 1},
        msg_id="dup-ephemeral-1",
        event_type=FederationEventType.PRESENCE_UPDATED,
    )
    r1 = await client.post("/federation/inbox/wh-test", data=body)
    assert r1.status == 200

    caplog.clear()
    with caplog.at_level(_logging.INFO, logger="socialhome.routes.federation"):
        r2 = await client.post("/federation/inbox/wh-test", data=body)
    assert r2.status == 410

    # No WARNING — the replay is expected noise on this event type.
    warnings = [
        rec
        for rec in caplog.records
        if rec.levelno == _logging.WARNING
        and rec.name == "socialhome.routes.federation"
    ]
    assert not warnings, [r.getMessage() for r in warnings]
    # One INFO line at the demoted ``dedup`` level instead.
    infos = [
        rec
        for rec in caplog.records
        if rec.levelno == _logging.INFO
        and rec.name == "socialhome.routes.federation"
        and "dedup" in rec.getMessage()
    ]
    assert len(infos) == 1, [r.getMessage() for r in caplog.records]


async def test_inbound_replay_logs_warning_for_content_event(client, env, caplog):
    """A replayed content event (DM / post / moment / …) stays at
    WARNING — operators care if their DM apparently round-tripped
    twice, because that's a symptom of a real bug, not the routine
    transport failover."""
    import logging as _logging

    body = _build_envelope(
        own_iid=env["own_iid"],
        peer_kp=env["peer_kp"],
        session_key=env["session_key"],
        payload={"content": "hi"},
        msg_id="dup-content-1",
        event_type=FederationEventType.DM_MESSAGE,
    )
    r1 = await client.post("/federation/inbox/wh-test", data=body)
    assert r1.status == 200

    caplog.clear()
    with caplog.at_level(_logging.WARNING, logger="socialhome.routes.federation"):
        r2 = await client.post("/federation/inbox/wh-test", data=body)
    assert r2.status == 410

    warnings = [
        rec
        for rec in caplog.records
        if rec.levelno == _logging.WARNING
        and rec.name == "socialhome.routes.federation"
        and "rejected" in rec.getMessage()
    ]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]


# ─── Public path / no auth required ─────────────────────────────────────


async def test_inbound_does_not_require_bearer_token(client, env):
    """The inbox is in _DEFAULT_PUBLIC_PATHS — no Authorization needed."""
    body = _build_envelope(
        own_iid=env["own_iid"],
        peer_kp=env["peer_kp"],
        session_key=env["session_key"],
        payload={"user_id": "alice"},
        msg_id="no-auth-msg",
    )
    # No headers — auth middleware must let this through.
    r = await client.post("/federation/inbox/wh-test", data=body)
    assert r.status == 200


async def test_inbound_rate_limit_per_ip(client, env):
    """§Audit #4: a flood from one remote IP gets 429'd. We exercise the
    limiter directly rather than firing 1000 real HTTP requests so the
    test stays fast and deterministic. The route consults
    ``rate_limiter_key`` with ``federation-inbox:{client_ip}`` —
    pre-saturating that bucket forces the *next* POST to 429."""
    from socialhome.app_keys import rate_limiter_key
    from socialhome.routes.federation import (
        INBOX_RATE_LIMIT,
        INBOX_RATE_WINDOW_S,
    )

    limiter = client.server.app[rate_limiter_key]
    # The TestClient uses 127.0.0.1 — match what the route sees so the
    # bucket key collides.
    bucket = "federation-inbox:127.0.0.1"
    for _ in range(INBOX_RATE_LIMIT):
        assert limiter.is_allowed(
            bucket,
            limit=INBOX_RATE_LIMIT,
            window_s=INBOX_RATE_WINDOW_S,
        )

    body = _build_envelope(
        own_iid=env["own_iid"],
        peer_kp=env["peer_kp"],
        session_key=env["session_key"],
        payload={"user_id": "alice"},
        msg_id="rate-msg",
    )
    r = await client.post("/federation/inbox/wh-test", data=body)
    assert r.status == 429
    body_json = await r.json()
    assert body_json["error"] == "rate_limited"
    # The sender's outbox reads this to wait the window out instead of
    # dropping the envelope (a 429 is back-pressure, not a refusal).
    retry_after = int(r.headers["Retry-After"])
    assert 1 <= retry_after <= INBOX_RATE_WINDOW_S


async def test_inbound_error_does_not_leak_exception_text(client, env):
    """§Audit #7: validation rejections must NOT echo the underlying
    ``ValueError`` text to the wire — that exposes ban-list / replay
    cache state to an attacker probing inbox IDs."""
    # Replay the same msg_id twice — the second response should be 410
    # but with a generic body (no "Replay detected: msg_id=…").
    body = _build_envelope(
        own_iid=env["own_iid"],
        peer_kp=env["peer_kp"],
        session_key=env["session_key"],
        payload={"x": 1},
        msg_id="leak-test-1",
    )
    r1 = await client.post("/federation/inbox/wh-test", data=body)
    assert r1.status == 200
    r2 = await client.post("/federation/inbox/wh-test", data=body)
    assert r2.status == 410
    body_json = await r2.json()
    assert body_json == {"error": "gone"}
    assert "msg_id" not in str(body_json)
    assert "Replay" not in str(body_json)
