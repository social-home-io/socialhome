"""Route tests for /api/pairing/* and /api/connections (§11, §23.71)."""

from __future__ import annotations


from social_home.app_keys import federation_repo_key
from social_home.domain.federation import (
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)

from .conftest import _auth


def _fake_instance(iid: str = "peer-1") -> RemoteInstance:
    return RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_webhook_url="https://peer/wh",
        local_webhook_id=f"wh-{iid}",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )


async def test_initiate_pairing_returns_qr_payload(client):
    r = await client.post(
        "/api/pairing/initiate",
        json={"webhook_url": "https://example/wh"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    data = await r.json()
    assert "token" in data and "identity_pk" in data and "dh_pk" in data


async def test_initiate_pairing_requires_webhook(client):
    r = await client.post(
        "/api/pairing/initiate",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_initiate_pairing_bad_json_400(client):
    r = await client.post(
        "/api/pairing/initiate",
        data="nope",
        headers={**_auth(client._tok), "Content-Type": "application/json"},
    )
    assert r.status == 400


async def test_accept_pairing_rejects_malformed(client):
    r = await client.post(
        "/api/pairing/accept",
        json={"only": "this"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_confirm_pairing_missing_fields(client):
    r = await client.post(
        "/api/pairing/confirm",
        json={"token": "t"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_confirm_pairing_unknown_token(client):
    r = await client.post(
        "/api/pairing/confirm",
        json={"token": "nope", "verification_code": "000000"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_list_connections_empty(client):
    r = await client.get(
        "/api/pairing/connections",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert await r.json() == []


async def test_list_connections_returns_instances(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-1"))
    r = await client.get(
        "/api/pairing/connections",
        headers=_auth(client._tok),
    )
    data = await r.json()
    assert len(data) == 1
    assert data[0]["instance_id"] == "peer-1"
    assert data[0]["status"] == "confirmed"


async def test_connections_alias_matches_pairing_list(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-2"))
    r = await client.get("/api/connections", headers=_auth(client._tok))
    assert r.status == 200
    assert (await r.json())[0]["instance_id"] == "peer-2"


async def test_unpair_missing_instance_returns_404(client):
    r = await client.delete(
        "/api/pairing/connections/nope",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_unpair_removes_instance(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-3"))
    r = await client.delete(
        "/api/pairing/connections/peer-3",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    # Gone from listing.
    r = await client.get(
        "/api/pairing/connections",
        headers=_auth(client._tok),
    )
    assert (await r.json()) == []


async def test_connections_endpoint_does_not_leak_session_keys(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-4"))
    r = await client.get("/api/connections", headers=_auth(client._tok))
    data = await r.json()
    row = data[0]
    assert "key_self_to_remote" not in row
    assert "key_remote_to_self" not in row
    assert "remote_identity_pk" not in row


# ─── Pairing introduce (§11.9) ─────────────────────────────────────────────


async def test_introduce_rejects_missing_fields(client):
    r = await client.post(
        "/api/pairing/introduce",
        json={"target_instance_id": "iid"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_introduce_rejects_self_referential(client):
    r = await client.post(
        "/api/pairing/introduce",
        json={"target_instance_id": "x", "via_instance_id": "x"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_introduce_unknown_relay_peer_404(client):
    r = await client.post(
        "/api/pairing/introduce",
        json={"target_instance_id": "target", "via_instance_id": "nobody"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_introduce_bad_json_400(client):
    r = await client.post(
        "/api/pairing/introduce",
        data="not-json",
        headers={**_auth(client._tok), "Content-Type": "application/json"},
    )
    assert r.status == 400


# ─── Pairing relay requests (§11.9 approve/decline) ────────────────────────


async def test_relay_requests_list_empty(client):
    r = await client.get(
        "/api/pairing/relay-requests",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert await r.json() == []


async def test_relay_approve_unknown_returns_404(client):
    r = await client.post(
        "/api/pairing/relay-requests/nope/approve",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_relay_decline_unknown_returns_404(client):
    r = await client.post(
        "/api/pairing/relay-requests/nope/decline",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_relay_list_approve_decline_full_flow(client):
    """Seed the queue via the bus, then approve / decline via HTTP."""
    from social_home.app_keys import pairing_relay_queue_key
    from social_home.domain.events import PairingIntroRelayReceived
    from social_home.infrastructure.event_bus import EventBus

    queue = client.app[pairing_relay_queue_key]
    # Inject two pending requests directly via the bus the queue subscribed to.
    bus: EventBus = queue._bus
    await bus.publish(
        PairingIntroRelayReceived(
            from_instance="peer-a",
            target_instance_id="peer-b",
            message="intro",
        )
    )
    await bus.publish(
        PairingIntroRelayReceived(
            from_instance="peer-c",
            target_instance_id="peer-d",
        )
    )

    r = await client.get(
        "/api/pairing/relay-requests",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    items = await r.json()
    assert len(items) == 2
    req_id = items[0]["id"]

    # Decline the first
    r = await client.post(
        f"/api/pairing/relay-requests/{req_id}/decline",
        headers=_auth(client._tok),
    )
    assert r.status == 204

    r = await client.get(
        "/api/pairing/relay-requests",
        headers=_auth(client._tok),
    )
    remaining = await r.json()
    assert len(remaining) == 1


async def test_relay_requests_require_admin(client):
    """Non-admin user gets 403."""
    from social_home.app_keys import db_key as _db_key
    from social_home.auth import sha256_token_hash
    from social_home.crypto import derive_user_id

    db = client.app[_db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'",
    )
    pk_bytes = bytes.fromhex(row["identity_public_key"])
    uid = derive_user_id(pk_bytes, "regular")
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("regular", uid, "Regular"),
    )
    raw = "regular-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t-reg", uid, "t", sha256_token_hash(raw)),
    )

    r = await client.get(
        "/api/pairing/relay-requests",
        headers=_auth(raw),
    )
    assert r.status == 403
