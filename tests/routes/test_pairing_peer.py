"""Route tests for /api/pairing/peer-{accept,confirm} (§11)."""

from __future__ import annotations

import orjson

from socialhome.app_keys import federation_service_key
from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
)
from socialhome.federation.peer_pairing_client import sign_peer_body


async def _prepare_qr_session(client) -> dict:
    """Generate a QR on the test instance and return the raw payload."""
    r = await client.post("/api/pairing/initiate", json={})
    # The peer-accept path is public, no auth needed for that POST —
    # but /api/pairing/initiate itself still requires admin auth.
    assert r.status in (201, 200)
    return await r.json()


async def test_peer_accept_happy_path(client):
    """Well-formed peer-accept materialises the RemoteInstance."""
    # This test runs on A's side. We generate A's QR, then
    # synthesise B's identity / DH keys, sign a peer-accept body,
    # and POST to /api/pairing/peer-accept.
    from .conftest import _auth

    r = await client.post(
        "/api/pairing/initiate",
        json={},
        headers=_auth(client._tok),
    )
    qr = await r.json()
    token = qr["token"]

    b_id_kp = generate_identity_keypair()
    b_dh_kp = generate_x25519_keypair()
    body = {
        "token": token,
        "verification_code": "123456",
        "identity_pk": b_id_kp.public_key.hex(),
        "instance_id": derive_instance_id(b_id_kp.public_key),
        "dh_pk": b_dh_kp.public_key.hex(),
        "inbox_url": "https://peer.example/federation/inbox/wh-b",
        "display_name": "Peer B",
    }
    signed = sign_peer_body(body, own_identity_seed=b_id_kp.private_key)

    # NOTE: peer-accept is public — no auth header.
    r = await client.post(
        "/api/pairing/peer-accept",
        data=orjson.dumps(signed),
        headers={"Content-Type": "application/json"},
    )
    assert r.status == 200
    data = await r.json()
    assert data["ok"] is True
    assert data["instance_id"] == derive_instance_id(b_id_kp.public_key)
    assert data["replay"] is False

    # The RemoteInstance is now queryable.
    from socialhome.app_keys import federation_repo_key

    fed_repo = client.app[federation_repo_key]
    instances = await fed_repo.list_instances()
    assert len(instances) == 1
    assert instances[0].remote_inbox_url == body["inbox_url"]
    assert instances[0].status.value == "pending_received"


async def test_peer_accept_replay_is_idempotent(client):
    """A second identical peer-accept returns ``replay: true`` without
    duplicating the RemoteInstance.
    """
    from .conftest import _auth

    r = await client.post(
        "/api/pairing/initiate",
        json={},
        headers=_auth(client._tok),
    )
    qr = await r.json()
    token = qr["token"]

    b_id_kp = generate_identity_keypair()
    b_dh_kp = generate_x25519_keypair()
    body = {
        "token": token,
        "verification_code": "123456",
        "identity_pk": b_id_kp.public_key.hex(),
        "dh_pk": b_dh_kp.public_key.hex(),
        "inbox_url": "https://peer.example/federation/inbox/wh-b",
    }
    signed = sign_peer_body(body, own_identity_seed=b_id_kp.private_key)

    r1 = await client.post(
        "/api/pairing/peer-accept",
        data=orjson.dumps(signed),
        headers={"Content-Type": "application/json"},
    )
    assert r1.status == 200
    assert (await r1.json())["replay"] is False

    r2 = await client.post(
        "/api/pairing/peer-accept",
        data=orjson.dumps(signed),
        headers={"Content-Type": "application/json"},
    )
    assert r2.status == 200
    assert (await r2.json())["replay"] is True

    # Still exactly one RemoteInstance.
    from socialhome.app_keys import federation_repo_key

    fed_repo = client.app[federation_repo_key]
    assert len(await fed_repo.list_instances()) == 1


async def test_peer_accept_rejects_missing_fields(client):
    r = await client.post(
        "/api/pairing/peer-accept",
        data=orjson.dumps({"token": "x"}),
        headers={"Content-Type": "application/json"},
    )
    assert r.status == 400


async def test_peer_accept_rejects_unknown_token(client):
    b_id_kp = generate_identity_keypair()
    b_dh_kp = generate_x25519_keypair()
    body = {
        "token": "not-a-real-token",
        "verification_code": "000000",
        "identity_pk": b_id_kp.public_key.hex(),
        "dh_pk": b_dh_kp.public_key.hex(),
        "inbox_url": "https://peer/wh",
    }
    signed = sign_peer_body(body, own_identity_seed=b_id_kp.private_key)
    r = await client.post(
        "/api/pairing/peer-accept",
        data=orjson.dumps(signed),
        headers={"Content-Type": "application/json"},
    )
    assert r.status == 404


async def test_peer_accept_rejects_bad_signature(client):
    from .conftest import _auth

    r = await client.post(
        "/api/pairing/initiate",
        json={},
        headers=_auth(client._tok),
    )
    qr = await r.json()

    b_id_kp = generate_identity_keypair()
    b_dh_kp = generate_x25519_keypair()
    body = {
        "token": qr["token"],
        "verification_code": "123456",
        "identity_pk": b_id_kp.public_key.hex(),
        "dh_pk": b_dh_kp.public_key.hex(),
        "inbox_url": "https://peer/wh",
        "signature": "00" * 64,  # garbage
    }
    r = await client.post(
        "/api/pairing/peer-accept",
        data=orjson.dumps(body),
        headers={"Content-Type": "application/json"},
    )
    assert r.status == 403


async def test_peer_accept_rejects_instance_id_mismatch(client):
    from .conftest import _auth

    r = await client.post(
        "/api/pairing/initiate",
        json={},
        headers=_auth(client._tok),
    )
    qr = await r.json()
    b_id_kp = generate_identity_keypair()
    b_dh_kp = generate_x25519_keypair()
    body = {
        "token": qr["token"],
        "verification_code": "123456",
        "identity_pk": b_id_kp.public_key.hex(),
        "instance_id": "not-derived-from-the-pk",
        "dh_pk": b_dh_kp.public_key.hex(),
        "inbox_url": "https://peer/wh",
    }
    signed = sign_peer_body(body, own_identity_seed=b_id_kp.private_key)
    r = await client.post(
        "/api/pairing/peer-accept",
        data=orjson.dumps(signed),
        headers={"Content-Type": "application/json"},
    )
    assert r.status == 403


async def test_peer_accept_bad_json_400(client):
    r = await client.post(
        "/api/pairing/peer-accept",
        data=b"not-json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status == 400


async def test_peer_confirm_requires_fields(client):
    r = await client.post(
        "/api/pairing/peer-confirm",
        data=orjson.dumps({}),
        headers={"Content-Type": "application/json"},
    )
    assert r.status == 400


async def test_peer_confirm_unknown_token(client):
    # Valid-shape body but the server doesn't know the token →
    # 404 NOT_FOUND.
    a_id_kp = generate_identity_keypair()
    body = {"token": "nope", "instance_id": "iid"}
    signed = sign_peer_body(body, own_identity_seed=a_id_kp.private_key)
    r = await client.post(
        "/api/pairing/peer-confirm",
        data=orjson.dumps(signed),
        headers={"Content-Type": "application/json"},
    )
    assert r.status == 404


async def test_peer_accept_is_public_no_auth_header_required(client):
    """Sanity: the path is on _DEFAULT_PUBLIC_PATHS — a request with
    no ``Authorization`` header reaches the handler (which then
    rejects on its own for missing fields).
    """
    r = await client.post(
        "/api/pairing/peer-accept",
        data=orjson.dumps({}),
        headers={"Content-Type": "application/json"},
        # explicitly no Authorization header
    )
    # 400 (missing fields), not 401 (would mean auth blocked us).
    assert r.status == 400


async def test_coordinator_exposes_peer_pairing_client(client):
    """The service has wired the outbound client so ``accept`` /
    ``confirm`` can deliver peer-accept / peer-confirm.
    """
    svc = client.app[federation_service_key]
    assert svc._pairing._peer_pairing_client is not None
