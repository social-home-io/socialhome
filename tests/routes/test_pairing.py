"""Route tests for /api/pairing/* and /api/connections (§11, §23.71)."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import orjson

from socialhome.app_keys import (
    auto_pair_coordinator_key,
    capabilities_outbound_key,
    db_key as _db_key,
    dm_routing_service_key,
    event_bus_key,
    federation_repo_key,
    federation_service_key,
    federation_transport_key,
    gfs_connection_repo_key,
    gfs_connection_service_key,
    instance_keywrap_public_key_key,
    key_manager_key,
    outbox_repo_key,
    peer_gfs_relay_service_key,
    peer_home_sharing_service_key,
    peer_unpair_service_key,
    platform_adapter_key,
)
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.domain.events import PeerUnpaired
from socialhome.domain.federation import (
    FederationEventType,
    GfsConnection,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.repositories.dm_routing_repo import SqliteDmRoutingRepo
from socialhome.services.dm_routing_service import DmRoutingService

from .conftest import _auth


async def _seed_relay(svc: DmRoutingService, *, target: str, via: str, ts: str) -> None:
    """Seed a relay path row into the SQLite repo for test fixtures.

    Calls the test-only :meth:`SqliteDmRoutingRepo.insert_relay_path_for_test`
    directly on the concrete repo — keeping production service code free of
    test-seeding logic.  The cast lives here (test boundary) where it belongs.
    """
    repo = svc._repo
    assert isinstance(repo, SqliteDmRoutingRepo), (
        "_seed_relay requires SqliteDmRoutingRepo"
    )
    await repo.insert_relay_path_for_test(
        conversation_id=f"test-relay-{target}",
        sender_user_id="test-sender",
        target_instance=target,
        via=via,
        ts=ts,
    )


def _fake_instance(iid: str = "peer-1") -> RemoteInstance:
    return RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url="https://peer/wh",
        local_inbox_id=f"wh-{iid}",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )


async def test_initiate_pairing_returns_qr_payload(client):
    # Body is empty — server sources the inbox base URL from the
    # platform adapter (seeded via [standalone].external_url in
    # tests/routes/conftest.py).
    r = await client.post(
        "/api/pairing/initiate",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    data = await r.json()
    assert "token" in data and "identity_pk" in data and "dh_pk" in data
    # Advertised URL = seeded base + "/" + generated own_local_inbox_id.
    assert data["inbox_url"].startswith(
        "https://test.example/federation/inbox/",
    )


async def test_initiate_pairing_not_configured_when_base_missing(
    tmp_dir, aiohttp_client
):
    """422 NOT_CONFIGURED when [standalone].external_url is unset."""
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
    )
    from socialhome.app import create_app
    from socialhome.app_keys import db_key as _db_key
    from socialhome.auth import sha256_token_hash
    from socialhome.crypto import derive_user_id

    app = create_app(cfg)
    tc = await aiohttp_client(app)
    db = app[_db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'"
    )
    pk_bytes = bytes.fromhex(row["identity_public_key"])
    uid = derive_user_id(pk_bytes, "admin")
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,1)",
        ("admin", uid, "Admin"),
    )
    raw = "tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t1", uid, "t", sha256_token_hash(raw)),
    )
    r = await tc.post(
        "/api/pairing/initiate",
        json={},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status == 422
    body = await r.json()
    assert body["error"]["code"] == "NOT_CONFIGURED"


async def test_initiate_pairing_bad_json_still_ok(client):
    """Unparseable body used to be 400 — now the body is ignored entirely,
    so the route proceeds on the adapter-provided base and returns 201.
    """
    r = await client.post(
        "/api/pairing/initiate",
        data="nope",
        headers={**_auth(client._tok), "Content-Type": "application/json"},
    )
    assert r.status == 201


# ── initiate: the code's reach (url / url_gfs / gfs) ──


async def _connect_gfs(client, cid: str, url: str, *, relays: bool = True) -> None:
    """Seed an active GFS connection; ``relays`` marks its signed
    ``envelope_relay`` capability as verified (no /gfs/info fetch)."""
    await client.app[gfs_connection_repo_key].save(
        GfsConnection(
            id=cid,
            gfs_instance_id=f"pinned-{cid}",
            display_name=cid,
            public_key="ab" * 32,
            inbox_url=url,
            status="active",
            paired_at="2026-01-01T00:00:00+00:00",
        )
    )
    if relays:
        client.app[gfs_connection_service_key]._envelope_relay[cid] = True


async def _initiate(client, body=None):
    return await client.post(
        "/api/pairing/initiate",
        json=body if body is not None else {},
        headers=_auth(client._tok),
    )


async def test_initiate_url_reach_carries_no_gfs_information(client):
    await _connect_gfs(client, "g1", "https://gfs-1.example")
    for body in ({}, {"reach": "url"}):
        r = await _initiate(client, body)
        assert r.status == 201
        data = await r.json()
        assert data["inbox_url"].startswith("https://test.example/federation/inbox/")
        for key in ("reach", "gfs", "keywrap_pk", "keywrap_sig", "keywrap_suite"):
            assert key not in data
        assert "gfs-1" not in str(data)


async def test_initiate_gfs_reach_names_one_relay_capable_gfs(client):
    await _connect_gfs(client, "g0", "https://gfs-0.example", relays=False)
    await _connect_gfs(client, "g1", "https://gfs-1.example")
    r = await _initiate(client, {"reach": "gfs"})
    assert r.status == 201
    data = await r.json()
    assert data["reach"] == "gfs"
    assert data["inbox_url"] == ""
    assert data["gfs"] == {"url": "https://gfs-1.example", "instance_id": "pinned-g1"}
    assert data["keywrap_pk"] == client.app[instance_keywrap_public_key_key].hex()
    assert data["keywrap_suite"] == "x25519"
    assert data["keywrap_sig"]
    assert "gfs-0" not in str(data)


async def test_initiate_url_gfs_reach_uses_the_chosen_gfs(client):
    await _connect_gfs(client, "g1", "https://gfs-1.example")
    await _connect_gfs(client, "g2", "https://gfs-2.example")
    r = await _initiate(client, {"reach": "url_gfs", "gfs_id": "g2"})
    assert r.status == 201
    data = await r.json()
    assert data["reach"] == "url_gfs"
    assert data["inbox_url"].startswith("https://test.example/federation/inbox/")
    assert data["gfs"]["url"] == "https://gfs-2.example"
    # An unknown gfs_id falls back to the first eligible connection.
    r = await _initiate(client, {"reach": "url_gfs", "gfs_id": "nope"})
    assert (await r.json())["gfs"]["url"] == "https://gfs-1.example"


async def test_initiate_gfs_reach_without_a_relay_capable_gfs_is_422(client):
    await _connect_gfs(client, "g0", "https://gfs-0.example", relays=False)
    for reach in ("gfs", "url_gfs"):
        r = await _initiate(client, {"reach": reach})
        assert r.status == 422
        assert (await r.json())["error"]["code"] == "GFS_NOT_CONNECTED"


async def test_initiate_unknown_reach_is_422(client):
    r = await _initiate(client, {"reach": "pigeon"})
    assert r.status == 422
    assert (await r.json())["error"]["code"] == "INVALID_REACH"


async def test_initiate_non_object_body_is_the_classic_code(client):
    r = await _initiate(client, ["gfs"])
    assert r.status == 201
    assert "reach" not in await r.json()


async def test_initiate_without_a_url_only_the_gfs_reach_works(client):
    class _NoBase:
        async def get_federation_base(self):
            return None

    await _connect_gfs(client, "g1", "https://gfs-1.example")
    client.app[platform_adapter_key] = _NoBase()
    for reach in ("url", "url_gfs"):
        r = await _initiate(client, {"reach": reach})
        assert r.status == 422
        assert (await r.json())["error"]["code"] == "NOT_CONFIGURED"
    r = await _initiate(client, {"reach": "gfs"})
    assert r.status == 201
    assert (await r.json())["inbox_url"] == ""


def _gfs_code(*, gfs_url: str, bind: bool = True) -> dict:
    """A ``gfs``-reach pairing code from a household we have never met."""
    kp = generate_identity_keypair()
    keywrap = generate_x25519_keypair()
    signer = kp.private_key if bind else generate_identity_keypair().private_key
    return {
        "token": "tok-gfs",
        "instance_id": derive_instance_id(kp.public_key),
        "identity_pk": kp.public_key.hex(),
        "dh_pk": generate_x25519_keypair().public_key.hex(),
        "inbox_url": "",
        "reach": "gfs",
        "gfs": {"url": gfs_url, "instance_id": "pinned-elsewhere"},
        "keywrap_pk": keywrap.public_key.hex(),
        "keywrap_sig": b64url_encode(sign_ed25519(signer, keywrap.public_key)),
        "keywrap_suite": "x25519",
    }


async def test_accept_gfs_code_from_a_gfs_we_are_not_on_is_422(client):
    await _connect_gfs(client, "g1", "https://gfs-1.example")
    r = await client.post(
        "/api/pairing/accept",
        json=_gfs_code(gfs_url="https://other-gfs.example"),
        headers=_auth(client._tok),
    )
    assert r.status == 422
    assert (await r.json())["error"]["code"] == "GFS_NOT_SHARED"
    assert await client.app[federation_repo_key].list_instances() == []


async def test_accept_gfs_code_with_an_unbound_keywrap_key_is_422(client):
    await _connect_gfs(client, "g1", "https://gfs-1.example")
    r = await client.post(
        "/api/pairing/accept",
        json=_gfs_code(gfs_url="https://gfs-1.example", bind=False),
        headers=_auth(client._tok),
    )
    assert r.status == 422
    assert (await r.json())["error"]["code"] == "KEYWRAP_INVALID"
    assert await client.app[federation_repo_key].list_instances() == []


async def test_accept_pairing_rejects_malformed(client):
    r = await client.post(
        "/api/pairing/accept",
        json={"only": "this"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_accept_pairing_rejects_invalid_household_address(client):
    """A pairing code whose inbox address is not a usable http(s) household
    URL is refused up front with a specific, admin-readable error — and
    nothing is stored for it."""
    peer = generate_identity_keypair()
    r = await client.post(
        "/api/pairing/accept",
        json={
            "token": "tok-bad-url",
            "identity_pk": peer.public_key.hex(),
            "dh_pk": "11" * 32,
            "inbox_url": "file:///etc/passwd",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 422
    body = await r.json()
    assert body["error"]["code"] == "INVALID_PEER_URL"
    assert "household address" in body["error"]["detail"]
    fed_repo = client.server.app[federation_repo_key]
    assert await fed_repo.get_instance(derive_instance_id(peer.public_key)) is None
    assert await fed_repo.get_pairing("tok-bad-url") is None


class _RecordingPeerClient:
    """Stands in for the outbound peer-accept POST so a test can see
    whether the household tried to call the scanned inbox URL."""

    def __init__(self) -> None:
        self.accepts: list[str] = []

    async def send_peer_accept(self, *, peer_inbox_url, body):
        self.accepts.append(peer_inbox_url)

        class _R:
            ok = True
            status_code = 200
            error = None

        return _R()


def _record_outbound(client) -> _RecordingPeerClient:
    rec = _RecordingPeerClient()
    fed = client.server.app[federation_service_key]
    fed._pairing.attach_peer_pairing_client(rec)
    return rec


def _scanned_code(token: str) -> tuple[dict, str]:
    peer = generate_identity_keypair()
    peer_id = derive_instance_id(peer.public_key)
    return (
        {
            "token": token,
            "instance_id": peer_id,
            "identity_pk": peer.public_key.hex(),
            "dh_pk": generate_x25519_keypair().public_key.hex(),
            "inbox_url": "https://peer.example/federation/inbox/abc",
            "display_name": "Peer",
        },
        peer_id,
    )


async def _seed_member(db, username: str = "bob") -> str:
    """Seed a signed-in, non-admin household user; return their token."""
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        (username, f"{username}-id", username.capitalize()),
    )
    raw = f"{username}-raw-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        (f"tok-{username}", f"{username}-id", "t", sha256_token_hash(raw)),
    )
    return raw


async def test_accept_pairing_requires_sign_in(client):
    """Starting a pairing from a scanned code is a signed-in admin action:
    without credentials nothing is stored and nothing is sent anywhere."""
    rec = _record_outbound(client)
    code, peer_id = _scanned_code("tok-anon")
    r = await client.post("/api/pairing/accept", json=code)
    assert r.status == 401
    fed_repo = client.server.app[federation_repo_key]
    assert await fed_repo.get_instance(peer_id) is None
    assert await fed_repo.get_pairing("tok-anon") is None
    assert rec.accepts == []


async def test_accept_pairing_requires_admin(client):
    rec = _record_outbound(client)
    member_tok = await _seed_member(client._db)
    code, peer_id = _scanned_code("tok-member")
    r = await client.post(
        "/api/pairing/accept",
        json=code,
        headers=_auth(member_tok),
    )
    assert r.status == 403
    fed_repo = client.server.app[federation_repo_key]
    assert await fed_repo.get_instance(peer_id) is None
    assert await fed_repo.get_pairing("tok-member") is None
    assert rec.accepts == []


async def test_accept_pairing_as_admin_stores_and_notifies_peer(client):
    rec = _record_outbound(client)
    code, peer_id = _scanned_code("tok-admin")
    r = await client.post(
        "/api/pairing/accept",
        json=code,
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["token"] == "tok-admin"
    assert len(body["verification_code"]) == 6
    fed_repo = client.server.app[federation_repo_key]
    inst = await fed_repo.get_instance(peer_id)
    assert inst is not None
    assert inst.status is PairingStatus.PENDING_RECEIVED
    assert rec.accepts == ["https://peer.example/federation/inbox/abc"]


async def test_initiate_pairing_requires_admin(client):
    member_tok = await _seed_member(client._db)
    r = await client.post(
        "/api/pairing/initiate",
        json={},
        headers=_auth(member_tok),
    )
    assert r.status == 403


async def test_confirm_pairing_requires_admin(client):
    member_tok = await _seed_member(client._db)
    r = await client.post(
        "/api/pairing/confirm",
        json={"token": "t", "verification_code": "000000"},
        headers=_auth(member_tok),
    )
    assert r.status == 403


class _SendRecorder:
    """Replaces the federation service so a test sees what was sent."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def stop(self) -> None:
        """App cleanup cancels deferred mesh re-sends; nothing to cancel."""

    async def send_event(self, *, to_instance_id, event_type, payload):
        self.sent.append(
            {"to": to_instance_id, "type": event_type, "payload": payload},
        )

        class _R:
            ok = True

        return _R()


def _record_sends(client) -> _SendRecorder:
    rec = _SendRecorder()
    client.app[federation_service_key] = rec
    return rec


async def test_introduce_requires_admin(client):
    """Asking a peer to introduce this household is a trust decision."""
    await client.app[federation_repo_key].save_instance(_fake_instance("via-1"))
    rec = _record_sends(client)
    member_tok = await _seed_member(client._db)
    r = await client.post(
        "/api/pairing/introduce",
        json={"target_instance_id": "target-1", "via_instance_id": "via-1"},
        headers=_auth(member_tok),
    )
    assert r.status == 403
    assert rec.sent == []


async def test_introduce_as_admin_sends_intro_relay(client):
    await client.app[federation_repo_key].save_instance(_fake_instance("via-2"))
    rec = _record_sends(client)
    r = await client.post(
        "/api/pairing/introduce",
        json={
            "target_instance_id": "target-2",
            "via_instance_id": "via-2",
            "message": "hi",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 204
    assert rec.sent == [
        {
            "to": "via-2",
            "type": FederationEventType.PAIRING_INTRO_RELAY,
            "payload": {"target_instance_id": "target-2", "message": "hi"},
        }
    ]


async def test_auto_pair_via_requires_admin(client, monkeypatch):
    await client.app[federation_repo_key].save_instance(_fake_instance("via-3"))
    coord = client.app[auto_pair_coordinator_key]
    calls: list[dict] = []

    async def _request_via(self, **kwargs):
        calls.append(kwargs)
        return {"request_id": "r"}

    monkeypatch.setattr(type(coord), "request_via", _request_via)
    member_tok = await _seed_member(client._db)
    r = await client.post(
        "/api/pairing/auto-pair-via",
        json={"via_instance_id": "via-3", "target_instance_id": "target-3"},
        headers=_auth(member_tok),
    )
    assert r.status == 403
    assert calls == []
    # Same body as an admin reaches the coordinator (guard is the only gate).
    r = await client.post(
        "/api/pairing/auto-pair-via",
        json={"via_instance_id": "via-3", "target_instance_id": "target-3"},
        headers=_auth(client._tok),
    )
    assert r.status == 202
    assert [c["via_instance_id"] for c in calls] == ["via-3"]


async def test_unpair_requires_admin(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-keep"))
    member_tok = await _seed_member(client._db)
    r = await client.delete(
        "/api/pairing/connections/peer-keep",
        headers=_auth(member_tok),
    )
    assert r.status == 403
    assert await fed_repo.get_instance("peer-keep") is not None


async def test_unpair_requires_sign_in(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-keep-2"))
    r = await client.delete("/api/pairing/connections/peer-keep-2")
    assert r.status == 401
    assert await fed_repo.get_instance("peer-keep-2") is not None


async def test_list_connections_stays_readable_for_members(client):
    """The dashboard network map shows every signed-in member the paired
    households read-only, so the listing is not admin-gated."""
    await client.app[federation_repo_key].save_instance(_fake_instance("peer-ro"))
    member_tok = await _seed_member(client._db)
    r = await client.get("/api/connections", headers=_auth(member_tok))
    assert r.status == 200
    assert [row["instance_id"] for row in await r.json()] == ["peer-ro"]


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


async def test_unpair_publishes_peer_unpaired(client):
    """A local unpair publishes ``PeerUnpaired`` so the realtime bridge
    pushes ``connection.removed`` to every open connections list."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-5"))
    seen: list[PeerUnpaired] = []
    client.app[event_bus_key].subscribe(PeerUnpaired, seen.append)
    r = await client.delete(
        "/api/pairing/connections/peer-5",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert [e.instance_id for e in seen] == ["peer-5"]


async def test_unpair_404_publishes_nothing(client):
    seen: list[PeerUnpaired] = []
    client.app[event_bus_key].subscribe(PeerUnpaired, seen.append)
    r = await client.delete(
        "/api/pairing/connections/nope",
        headers=_auth(client._tok),
    )
    assert r.status == 404
    assert seen == []


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
    from socialhome.app_keys import pairing_relay_queue_key
    from socialhome.domain.events import PairingIntroRelayReceived
    from socialhome.infrastructure.event_bus import EventBus

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


# ── /api/pairing/connections/{id}/visible-users ────────────────────────


async def _seed_extra_user(client, username: str) -> str:
    """Create a second local user, return their user_id."""
    from socialhome.app_keys import db_key as _db_key
    from socialhome.crypto import derive_user_id

    db = client.app[_db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'",
    )
    pk_bytes = bytes.fromhex(row["identity_public_key"])
    uid = derive_user_id(pk_bytes, username)
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        (username, uid, username.title()),
    )
    return uid


async def test_visible_users_get_returns_default_visible(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-vis-1"))
    extra_uid = await _seed_extra_user(client, "lily")

    r = await client.get(
        "/api/pairing/connections/peer-vis-1/visible-users",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    rows = {u["user_id"]: u for u in body["users"]}
    # Both admin and the freshly-seeded ``lily`` default to visible.
    assert client._uid in rows and rows[client._uid]["visible"] is True
    assert extra_uid in rows and rows[extra_uid]["visible"] is True


async def test_visible_users_patch_hides_user_and_sends_user_removed(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-vis-2"))
    extra_uid = await _seed_extra_user(client, "kai")

    captured: list[dict] = []

    class _Recorder:
        async def stop(self) -> None:
            """App cleanup cancels deferred mesh re-sends; nothing to cancel."""

        async def send_event(self, *, to_instance_id, event_type, payload):
            captured.append(
                {"to": to_instance_id, "type": event_type, "payload": payload},
            )

    from socialhome.app_keys import federation_service_key

    client.app[federation_service_key] = _Recorder()

    r = await client.patch(
        "/api/pairing/connections/peer-vis-2/visible-users",
        json={"updates": [{"user_id": extra_uid, "visible": False}]},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    rows = {u["user_id"]: u for u in body["users"]}
    assert rows[extra_uid]["visible"] is False

    from socialhome.domain.federation import FederationEventType

    assert len(captured) == 1
    assert captured[0]["to"] == "peer-vis-2"
    assert captured[0]["type"] is FederationEventType.USER_REMOVED
    assert captured[0]["payload"] == {"user_id": extra_uid}


async def test_visible_users_patch_unhide_sends_user_updated(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-vis-3"))
    extra_uid = await _seed_extra_user(client, "max")

    from socialhome.app_keys import (
        federation_service_key,
        peer_user_visibility_repo_key,
    )

    # Pre-hide the user via the repo so the PATCH path tests the
    # hidden→visible transition specifically.
    vis_repo = client.app[peer_user_visibility_repo_key]
    await vis_repo.set_visibility(
        instance_id="peer-vis-3",
        user_id=extra_uid,
        visible=False,
        set_by=None,
    )

    captured: list[dict] = []

    class _Recorder:
        async def stop(self) -> None:
            """App cleanup cancels deferred mesh re-sends; nothing to cancel."""

        async def send_event(self, *, to_instance_id, event_type, payload):
            captured.append({"type": event_type, "payload": payload})

        async def peer_supports(self, instance_id, *, min_version):
            return False

    client.app[federation_service_key] = _Recorder()

    r = await client.patch(
        "/api/pairing/connections/peer-vis-3/visible-users",
        json={"updates": [{"user_id": extra_uid, "visible": True}]},
        headers=_auth(client._tok),
    )
    assert r.status == 200

    from socialhome.domain.federation import FederationEventType

    assert len(captured) == 1
    assert captured[0]["type"] is FederationEventType.USER_UPDATED
    assert captured[0]["payload"]["user_id"] == extra_uid


async def test_visible_users_patch_no_op_when_already_in_target_state(client):
    """Visible-→visible flip is a no-op (already-visible default)."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-vis-4"))
    extra_uid = await _seed_extra_user(client, "noop")

    captured: list[dict] = []

    class _Recorder:
        async def stop(self) -> None:
            """App cleanup cancels deferred mesh re-sends; nothing to cancel."""

        async def send_event(self, *, to_instance_id, event_type, payload):
            captured.append({})

    from socialhome.app_keys import federation_service_key

    client.app[federation_service_key] = _Recorder()

    r = await client.patch(
        "/api/pairing/connections/peer-vis-4/visible-users",
        json={"updates": [{"user_id": extra_uid, "visible": True}]},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    # Already-visible to already-visible — no envelope sent.
    assert captured == []


async def test_visible_users_patch_rejects_unknown_user(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-vis-5"))

    r = await client.patch(
        "/api/pairing/connections/peer-vis-5/visible-users",
        json={"updates": [{"user_id": "not-a-real-user", "visible": False}]},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_visible_users_404_for_unknown_peer(client):
    r = await client.get(
        "/api/pairing/connections/no-such-peer/visible-users",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_visible_users_requires_admin(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-vis-6"))

    from socialhome.app_keys import db_key as _db_key

    db = client.app[_db_key]
    await db.enqueue(
        "UPDATE users SET is_admin=0 WHERE user_id=?",
        (client._uid,),
    )
    r = await client.get(
        "/api/pairing/connections/peer-vis-6/visible-users",
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_relay_requests_require_admin(client):
    """Non-admin user gets 403."""
    from socialhome.app_keys import db_key as _db_key
    from socialhome.auth import sha256_token_hash
    from socialhome.crypto import derive_user_id

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


# ─── Local alias on a paired peer (PR A) ──────────────────────────────────


async def test_alias_patch_sets_alias_and_returns_effective_name(client):
    fed_repo = client.app[federation_repo_key]
    inst = _fake_instance("peer-alias-1")
    # Simulate the cryptic federated display_name the user actually
    # sees today (truncated instance_id).
    await fed_repo.save_instance(inst)

    r = await client.patch(
        "/api/pairing/connections/peer-alias-1/alias",
        json={"alias": "Brother's house"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["instance_id"] == "peer-alias-1"
    assert body["local_alias"] == "Brother's house"
    assert body["effective_display_name"] == "Brother's house"
    # Federated name unchanged — we only renamed locally.
    assert body["display_name"] == "peer-alias-1"

    # GET /api/pairing/connections now returns the effective name in
    # ``display_name`` (the SPA-facing field).
    listing = await (
        await client.get(
            "/api/pairing/connections",
            headers=_auth(client._tok),
        )
    ).json()
    row = next(r for r in listing if r["instance_id"] == "peer-alias-1")
    assert row["display_name"] == "Brother's house"
    assert row["federated_display_name"] == "peer-alias-1"
    assert row["local_alias"] == "Brother's house"


async def test_alias_patch_clear_with_null(client):
    """``{"alias": null}`` clears the alias; effective name falls back."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-alias-2"))
    await fed_repo.update_alias("peer-alias-2", "Temporary")

    r = await client.patch(
        "/api/pairing/connections/peer-alias-2/alias",
        json={"alias": None},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["local_alias"] is None
    assert body["effective_display_name"] == "peer-alias-2"


async def test_alias_patch_whitespace_clears(client):
    """Whitespace-only alias is treated as a clear — keeps the picker
    from showing a blank effective name."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-alias-3"))
    await fed_repo.update_alias("peer-alias-3", "Some name")

    r = await client.patch(
        "/api/pairing/connections/peer-alias-3/alias",
        json={"alias": "   "},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["local_alias"] is None


async def test_alias_patch_rejects_too_long(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-alias-4"))
    r = await client.patch(
        "/api/pairing/connections/peer-alias-4/alias",
        json={"alias": "x" * 81},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_alias_patch_rejects_non_string(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-alias-5"))
    r = await client.patch(
        "/api/pairing/connections/peer-alias-5/alias",
        json={"alias": 42},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_alias_patch_unknown_peer_returns_404(client):
    r = await client.patch(
        "/api/pairing/connections/no-such-peer/alias",
        json={"alias": "Anything"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_alias_patch_requires_admin(client):
    """Non-admin token is rejected — local rename is an admin
    concern, same gate the other connection-edit views use."""
    from socialhome.app_keys import db_key as _db_key
    from socialhome.auth import sha256_token_hash
    from socialhome.crypto import derive_user_id

    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-alias-6"))

    db = client.app[_db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'",
    )
    pk_bytes = bytes.fromhex(row["identity_public_key"])
    uid = derive_user_id(pk_bytes, "member")
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("member", uid, "Member"),
    )
    raw = "member-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t-mem", uid, "t", sha256_token_hash(raw)),
    )

    r = await client.patch(
        "/api/pairing/connections/peer-alias-6/alias",
        json={"alias": "nope"},
        headers=_auth(raw),
    )
    assert r.status == 403


async def test_connections_response_carries_transport_rtc(client):
    """A confirmed peer whose DataChannel is open reports transport='rtc'."""
    kp = generate_identity_keypair()
    peer = RemoteInstance(
        id=derive_instance_id(kp.public_key),
        display_name="peer-rtc",
        remote_identity_pk=kp.public_key.hex(),
        key_self_to_remote="k",
        key_remote_to_self="k",
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh-rtc",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(peer)

    class _AlwaysOpenTransport:
        def is_ready(self, instance_id):
            return True

    client.app[federation_transport_key] = _AlwaysOpenTransport()

    r = await client.get(
        "/api/connections",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    rows = await r.json()
    row = next(x for x in rows if x["instance_id"] == peer.id)
    assert row["transport"] == "rtc"


async def test_connections_response_reports_the_relay_for_a_link_joined_peer(
    client,
):
    """A household seated from an invite link has NO address and no RTC
    path — its envelopes ride the connection-server relay. Reporting it
    as ``"https"`` named a transport that literally cannot be used (its
    ``remote_inbox_url`` is the empty string by design), so the
    Connections page told an operator to debug an HTTPS inbox that was
    never going to exist."""
    kp = generate_identity_keypair()
    peer = RemoteInstance(
        id=derive_instance_id(kp.public_key),
        display_name="peer-relay",
        remote_identity_pk=kp.public_key.hex(),
        key_self_to_remote="k",
        key_remote_to_self="k",
        remote_inbox_url="",
        local_inbox_id="wh-relay",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.SPACE_SESSION,
        relay_via="https://gfs.example.org",
        remote_keywrap_pk="cc" * 32,
    )
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(peer)

    class _NeverOpenTransport:
        def is_ready(self, instance_id):
            return False

    client.app[federation_transport_key] = _NeverOpenTransport()

    r = await client.get("/api/connections", headers=_auth(client._tok))
    rows = await r.json()
    row = next(x for x in rows if x["instance_id"] == peer.id)
    assert row["transport"] == "gfs_relay"


def _link_joined_peer(suffix: str) -> RemoteInstance:
    kp = generate_identity_keypair()
    return RemoteInstance(
        id=derive_instance_id(kp.public_key),
        display_name=f"peer-{suffix}",
        remote_identity_pk=kp.public_key.hex(),
        key_self_to_remote="k",
        key_remote_to_self="k",
        remote_inbox_url="",
        local_inbox_id=f"wh-{suffix}",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.SPACE_SESSION,
        relay_via="https://gfs.example.org",
        remote_keywrap_pk="cc" * 32,
    )


async def test_connections_show_a_peer_only_the_relay_has_accepted_for(client):
    """ "Accepted by the connection server" is not "delivered". A household
    whose recent traffic has only been handed to the relay reports
    ``relay_only`` plus the last acceptance time, so the operator can tell
    it apart from one that is actually receiving."""
    peer = _link_joined_peer("relay-only")
    await client.app[federation_repo_key].save_instance(peer)
    client.app[federation_service_key].note_relay_accepted(peer.id)

    r = await client.get("/api/connections", headers=_auth(client._tok))
    row = next(x for x in await r.json() if x["instance_id"] == peer.id)
    assert row["relay_only"] is True
    assert row["last_relay_accepted_at"]
    assert row["last_reachable_at"] is None


async def test_connections_relay_only_clears_once_delivery_is_proven(client):
    peer = _link_joined_peer("relay-proven")
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(peer)
    client.app[federation_service_key].note_relay_accepted(peer.id)
    # An inbound envelope from them (or a direct delivery) proves reach.
    await fed_repo.mark_reachable(peer.id)

    r = await client.get("/api/connections", headers=_auth(client._tok))
    row = next(x for x in await r.json() if x["instance_id"] == peer.id)
    assert row["relay_only"] is False
    assert row["last_relay_accepted_at"]


async def test_connections_without_relay_traffic_report_no_relay_state(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-no-relay"))

    r = await client.get("/api/connections", headers=_auth(client._tok))
    row = next(x for x in await r.json() if x["instance_id"] == "peer-no-relay")
    assert row["relay_only"] is False
    assert row["last_relay_accepted_at"] is None


async def test_connections_response_transport_https_when_channel_down(client):
    """Same peer, transport service reports not-ready → transport='https'."""
    kp = generate_identity_keypair()
    peer = RemoteInstance(
        id=derive_instance_id(kp.public_key),
        display_name="peer-https",
        remote_identity_pk=kp.public_key.hex(),
        key_self_to_remote="k",
        key_remote_to_self="k",
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh-https",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(peer)

    class _NeverOpenTransport:
        def is_ready(self, instance_id):
            return False

    client.app[federation_transport_key] = _NeverOpenTransport()

    r = await client.get(
        "/api/connections",
        headers=_auth(client._tok),
    )
    rows = await r.json()
    row = next(x for x in rows if x["instance_id"] == peer.id)
    assert row["transport"] == "https"


async def test_connections_response_transport_null_when_unreachable(client):
    """An unreachable confirmed peer has transport=null — we don't claim
    a transport for a peer we can't reach."""
    kp = generate_identity_keypair()
    peer = RemoteInstance(
        id=derive_instance_id(kp.public_key),
        display_name="peer-unreach",
        remote_identity_pk=kp.public_key.hex(),
        key_self_to_remote="k",
        key_remote_to_self="k",
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh-unreach",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(peer)
    await fed_repo.mark_unreachable(peer.id)

    class _NeverOpenTransport:
        def is_ready(self, instance_id):
            return False

    client.app[federation_transport_key] = _NeverOpenTransport()

    r = await client.get(
        "/api/connections",
        headers=_auth(client._tok),
    )
    rows = await r.json()
    row = next(x for x in rows if x["instance_id"] == peer.id)
    assert row["transport"] is None


async def test_connections_response_transport_https_when_no_rtc_wired(client):
    """No federation_transport_key in app → confirmed peer still gets
    transport='https'. Models a deployment without RTC wiring (e.g. a
    stripped harness) where federation has to fall back to HTTPS-only.
    """
    kp = generate_identity_keypair()
    peer = RemoteInstance(
        id=derive_instance_id(kp.public_key),
        display_name="peer-no-rtc",
        remote_identity_pk=kp.public_key.hex(),
        key_self_to_remote="k",
        key_remote_to_self="k",
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh-no-rtc",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(peer)

    # Deliberately drop the transport key — simulate a no-RTC build.
    client.app[federation_transport_key] = None

    r = await client.get(
        "/api/connections",
        headers=_auth(client._tok),
    )
    rows = await r.json()
    row = next(x for x in rows if x["instance_id"] == peer.id)
    assert row["transport"] == "https"


# ─── Transport detail (Task 6) ───────────────────────────────────────────────


async def test_transport_detail_returns_recent_relay(client):
    """For a peer with a recent DM relay, the endpoint returns
    {last_relay: {via, ts}}."""
    svc = client.app[dm_routing_service_key]
    now = datetime.now(timezone.utc).isoformat()
    await _seed_relay(svc, target="peer-target", via="peer-relay", ts=now)

    r = await client.get(
        "/api/pairing/connections/peer-target/transport-detail",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["last_relay"] is not None
    assert body["last_relay"]["via"] == "peer-relay"
    assert body["last_relay"]["ts"]  # non-empty


async def test_transport_detail_returns_null_when_no_recent_relay(client):
    r = await client.get(
        "/api/pairing/connections/peer-no-relay/transport-detail",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["last_relay"] is None


async def test_transport_detail_shows_inbox_url_for_confirmed_direct_peer(client):
    """The Manage panel's "Inbox" row: the admin sees the address their
    household delivers to for a QR-paired (manual) confirmed peer."""
    peer = _fake_instance("peer-direct")
    await client.app[federation_repo_key].save_instance(peer)
    r = await client.get(
        f"/api/pairing/connections/{peer.id}/transport-detail",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["inbox_url"] == "https://peer/wh"


async def test_transport_detail_hides_inbox_url_for_space_session_peer(client):
    """A household met through an invite link must never learn — or be
    shown — another household's address (GFS shielding)."""
    # Even if a URL were somehow on the row, it must not be surfaced.
    peer = dataclasses.replace(
        _link_joined_peer("inbox-hidden"), remote_inbox_url="https://leak/wh"
    )
    await client.app[federation_repo_key].save_instance(peer)
    r = await client.get(
        f"/api/pairing/connections/{peer.id}/transport-detail",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["inbox_url"] is None


async def test_transport_detail_hides_inbox_url_for_relay_only_peer(client):
    peer = _fake_instance("peer-relay-only")
    await client.app[federation_repo_key].save_instance(peer)
    client.app[federation_service_key].note_relay_accepted(peer.id)
    r = await client.get(
        f"/api/pairing/connections/{peer.id}/transport-detail",
        headers=_auth(client._tok),
    )
    assert (await r.json())["inbox_url"] is None


async def test_transport_detail_inbox_url_null_for_unknown_or_pending_peer(client):
    pending = RemoteInstance(
        id="peer-pending",
        display_name="peer-pending",
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url="https://pending/wh",
        local_inbox_id="wh-pending",
        status=PairingStatus.PENDING_SENT,
        source=InstanceSource.MANUAL,
    )
    await client.app[federation_repo_key].save_instance(pending)
    for iid in ("peer-pending", "peer-unknown"):
        r = await client.get(
            f"/api/pairing/connections/{iid}/transport-detail",
            headers=_auth(client._tok),
        )
        assert r.status == 200
        assert (await r.json())["inbox_url"] is None


async def test_connections_listing_never_carries_inbox_url(client):
    """The listing is member-readable; the address stays off it."""
    peer = _fake_instance("peer-listing")
    await client.app[federation_repo_key].save_instance(peer)
    r = await client.get("/api/connections", headers=_auth(client._tok))
    for row in await r.json():
        assert "inbox_url" not in row
        assert "remote_inbox_url" not in row


async def test_transport_detail_admin_only(client):
    """Non-admin user gets 403."""
    db = client.app[_db_key]
    await db.enqueue(
        "UPDATE users SET is_admin=0 WHERE user_id=?",
        (client._uid,),
    )
    r = await client.get(
        "/api/pairing/connections/peer-x/transport-detail",
        headers=_auth(client._tok),
    )
    assert r.status == 403


# ─── PATCH /api/pairing/connections/{instance_id} share_home ────────────────


class _CapturingShareHomeSvc:
    """Stub for PeerHomeSharingService that records calls."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def set_share_home(
        self, instance_id: str, *, value: bool, set_by: str | None
    ) -> None:
        self.calls.append((instance_id, value, set_by))


async def test_patch_share_home_false_persists_and_calls_service(client):
    """PATCH {share_home: false} returns 200 and the service sets the value."""
    fed_repo = client.app[federation_repo_key]
    inst = _fake_instance("peer-sh-1")
    await fed_repo.save_instance(inst)

    stub = _CapturingShareHomeSvc()
    client.app[peer_home_sharing_service_key] = stub

    r = await client.patch(
        "/api/pairing/connections/peer-sh-1",
        json={"share_home": False},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["instance_id"] == "peer-sh-1"
    assert stub.calls == [("peer-sh-1", False, client._uid)]


async def test_patch_share_home_true_persists_and_calls_service(client):
    """PATCH {share_home: true} returns 200 and the service sets the value."""
    fed_repo = client.app[federation_repo_key]
    inst = _fake_instance("peer-sh-2")
    await fed_repo.save_instance(inst)

    stub = _CapturingShareHomeSvc()
    client.app[peer_home_sharing_service_key] = stub

    r = await client.patch(
        "/api/pairing/connections/peer-sh-2",
        json={"share_home": True},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["instance_id"] == "peer-sh-2"
    assert stub.calls == [("peer-sh-2", True, client._uid)]


async def test_patch_share_home_invalid_type_returns_400(client):
    """PATCH with a non-bool share_home returns 422 UNPROCESSABLE."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-sh-3"))

    r = await client.patch(
        "/api/pairing/connections/peer-sh-3",
        json={"share_home": "yes"},
        headers=_auth(client._tok),
    )
    assert r.status == 422
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


async def test_patch_share_home_requires_admin(client):
    """Non-admin user is rejected with 403."""
    from socialhome.auth import sha256_token_hash
    from socialhome.crypto import derive_user_id

    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-sh-4"))

    db = client.app[_db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'"
    )
    pk_bytes = bytes.fromhex(row["identity_public_key"])
    uid = derive_user_id(pk_bytes, "member-sh")
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("member-sh", uid, "Member"),
    )
    raw = "member-sh-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t-mem-sh", uid, "t", sha256_token_hash(raw)),
    )

    r = await client.patch(
        "/api/pairing/connections/peer-sh-4",
        json={"share_home": False},
        headers=_auth(raw),
    )
    assert r.status == 403


# ─── GET /api/connections includes share_home ───────────────────────────────


async def test_get_connections_includes_share_home(client):
    """GET /api/connections returns share_home on every row; default is True."""
    fed_repo = client.app[federation_repo_key]
    inst = _fake_instance("peer-sh-list-1")
    await fed_repo.save_instance(inst)

    r = await client.get(
        "/api/connections",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    rows = await r.json()
    row = next(x for x in rows if x["instance_id"] == "peer-sh-list-1")
    # Default value is True — RemoteInstance.share_home defaults to True.
    assert row["share_home"] is True


# ─── GET /api/connections includes queued_envelopes ────────────────────────


async def test_get_connections_reports_zero_queued_envelopes(client):
    """A peer with an empty outbox reports queued_envelopes=0.

    The field is always present so the SPA can branch on the number
    rather than on ``undefined``.
    """
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-q-empty"))

    r = await client.get("/api/connections", headers=_auth(client._tok))
    assert r.status == 200
    rows = await r.json()
    row = next(x for x in rows if x["instance_id"] == "peer-q-empty")
    assert row["queued_envelopes"] == 0


async def test_get_connections_counts_pending_outbox_envelopes(client):
    """queued_envelopes reflects the peer's own pending outbox backlog.

    A household that has been offline for weeks piles up undelivered
    envelopes; the count is what tells an admin the difference between a
    blip and a peer that has been gone since spring.
    """
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-q-backlog"))
    await fed_repo.save_instance(_fake_instance("peer-q-other"))

    outbox = client.app[outbox_repo_key]
    for _ in range(3):
        await outbox.enqueue(
            instance_id="peer-q-backlog",
            event_type=FederationEventType.SPACE_POST_CREATED,
            payload_json="{}",
        )
    await outbox.enqueue(
        instance_id="peer-q-other",
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload_json="{}",
    )

    r = await client.get("/api/connections", headers=_auth(client._tok))
    assert r.status == 200
    rows = await r.json()
    backlog = next(x for x in rows if x["instance_id"] == "peer-q-backlog")
    other = next(x for x in rows if x["instance_id"] == "peer-q-other")
    assert backlog["queued_envelopes"] == 3
    assert other["queued_envelopes"] == 1


async def test_delivered_envelopes_are_not_counted_as_queued(client):
    """Only ``pending`` rows count — delivered ones are not a backlog."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-q-delivered"))

    outbox = client.app[outbox_repo_key]
    entry_id = await outbox.enqueue(
        instance_id="peer-q-delivered",
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload_json="{}",
    )
    await outbox.mark_delivered(entry_id)

    r = await client.get("/api/connections", headers=_auth(client._tok))
    rows = await r.json()
    row = next(x for x in rows if x["instance_id"] == "peer-q-delivered")
    assert row["queued_envelopes"] == 0


# ─── dropped (permanently failed) envelopes ────────────────────────────────


async def _seed_backlog(client, iid: str, *, pending: int, failed: int) -> None:
    """Give ``iid`` a real outbox backlog: ``pending`` waiting, ``failed`` dead."""
    outbox = client.app[outbox_repo_key]
    for i in range(pending):
        await outbox.enqueue(
            instance_id=iid,
            event_type=FederationEventType.SPACE_POST_CREATED,
            payload_json="{}",
            msg_id=f"{iid}-p{i}",
        )
    for i in range(failed):
        eid = await outbox.enqueue(
            instance_id=iid,
            event_type=FederationEventType.SPACE_POST_CREATED,
            payload_json="{}",
            msg_id=f"{iid}-f{i}",
        )
        await outbox.mark_failed(eid)


async def test_get_connections_reports_dropped_envelopes(client):
    """``failed`` rows are permanently given up on — reported separately.

    The support case that motivated this: 263 failed + 56 pending. Showing
    only the 56 renders data loss as a reassuring "queued for delivery".
    """
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-dropped"))
    await _seed_backlog(client, "peer-dropped", pending=2, failed=3)

    r = await client.get("/api/connections", headers=_auth(client._tok))
    assert r.status == 200
    rows = await r.json()
    row = next(x for x in rows if x["instance_id"] == "peer-dropped")
    assert row["queued_envelopes"] == 2
    assert row["dropped_envelopes"] == 3


async def test_get_connections_reports_zero_dropped_envelopes(client):
    """The field is always present so the SPA branches on a number."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-nodrop"))

    r = await client.get("/api/connections", headers=_auth(client._tok))
    rows = await r.json()
    row = next(x for x in rows if x["instance_id"] == "peer-nodrop")
    assert row["dropped_envelopes"] == 0


async def test_patch_connection_reports_real_envelope_counts(client):
    """PATCH returns the peer's real backlog, not a placeholder ``0``."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-patch-counts"))
    await _seed_backlog(client, "peer-patch-counts", pending=2, failed=3)
    client.app[peer_home_sharing_service_key] = _CapturingShareHomeSvc()

    r = await client.patch(
        "/api/pairing/connections/peer-patch-counts",
        json={"share_home": False},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["queued_envelopes"] == 2
    assert body["dropped_envelopes"] == 3


class _StubConfirmSvc:
    """Federation service stub whose confirm_pairing returns a known peer."""

    def __init__(self, inst) -> None:
        self._inst = inst

    async def stop(self) -> None:
        """App cleanup cancels deferred mesh re-sends; nothing to cancel."""

    async def confirm_pairing(self, token: str, code: str):
        return self._inst

    def last_relay_accepted_at(self, instance_id: str) -> str | None:
        return None


async def test_confirm_pairing_reports_real_envelope_counts(client):
    """The confirm response carries the peer's real backlog, not ``0``.

    A re-confirm of a household that has been dark is exactly when the
    admin needs to see what piled up and what was dropped.
    """
    inst = _fake_instance("peer-confirm-counts")
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(inst)
    await _seed_backlog(client, "peer-confirm-counts", pending=2, failed=3)
    client.app[federation_service_key] = _StubConfirmSvc(inst)

    r = await client.post(
        "/api/pairing/confirm",
        json={"token": "t", "verification_code": "123456"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["instance_id"] == "peer-confirm-counts"
    assert body["queued_envelopes"] == 2
    assert body["dropped_envelopes"] == 3


# ─── Unpair tells the peer (UNPAIR) before forgetting it ──────────────────


class _CaptureTransport:
    """Stands in for :class:`FederationTransport`: records the envelope and
    whether the peer's row (session key + inbox URL) still existed when it
    went out."""

    def __init__(self, repo, *, hang: bool = False) -> None:
        self._repo = repo
        self._hang = hang
        self.sent: list[tuple[dict, bool]] = []

    async def send(self, *, instance, envelope_dict):
        present = await self._repo.get_instance(instance.id) is not None
        self.sent.append((envelope_dict, present))
        if self._hang:
            await asyncio.sleep(3600)
        return SimpleNamespace(ok=True, via="https", status_code=202, error=None)

    async def close_all(self) -> None:  # app cleanup closes the transport
        return None


def _keyed_instance(client, iid: str, session_key: bytes) -> RemoteInstance:
    wrapped = client.app[key_manager_key].encrypt(session_key)
    return dataclasses.replace(
        _fake_instance(iid),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
    )


async def test_unpair_sends_signed_unpair_before_deleting_the_row(client):
    fed_repo = client.app[federation_repo_key]
    fed_svc = client.app[federation_service_key]
    session_key = b"\x11" * 32
    await fed_repo.save_instance(_keyed_instance(client, "peer-u1", session_key))
    await client.app[outbox_repo_key].enqueue(
        instance_id="peer-u1",
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload_json="{}",
    )
    transport = _CaptureTransport(fed_repo)
    fed_svc._transport = transport

    r = await client.delete(
        "/api/pairing/connections/peer-u1",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert await r.json() == {"ok": True, "peer_notified": True}

    assert len(transport.sent) == 1
    env, row_present_at_send = transport.sent[0]
    assert row_present_at_send, "UNPAIR must go out while the peer's keys exist"
    assert env["event_type"] == FederationEventType.UNPAIR.value
    assert env["from_instance"] == fed_svc.own_instance_id
    assert env["to_instance"] == "peer-u1"
    # Encryption-first: nothing but the routing fields in plaintext.
    assert "payload" not in env
    plain = fed_svc._encoder.decrypt_payload(env["encrypted_payload"], session_key)
    assert orjson.loads(plain) == {}
    unsigned = {k: v for k, v in env.items() if k != "signatures"}
    assert fed_svc._encoder.verify_signatures_all(
        orjson.dumps(unsigned),
        suite=env["sig_suite"],
        signatures=env["signatures"],
        ed_public_key=fed_svc.own_identity_pk,
        pq_public_key=None,
    )

    assert await fed_repo.get_instance("peer-u1") is None
    assert await client.app[outbox_repo_key].count_pending_for("peer-u1") == 0


async def test_unpair_unreachable_peer_still_succeeds_promptly(client):
    fed_repo = client.app[federation_repo_key]
    fed_svc = client.app[federation_service_key]
    await fed_repo.save_instance(_keyed_instance(client, "peer-u2", b"\x12" * 32))
    fed_svc._transport = _CaptureTransport(fed_repo, hang=True)
    client.app[peer_unpair_service_key]._notify_timeout_s = 0.2
    seen: list[PeerUnpaired] = []
    client.app[event_bus_key].subscribe(PeerUnpaired, seen.append)

    started = time.monotonic()
    r = await client.delete(
        "/api/pairing/connections/peer-u2",
        headers=_auth(client._tok),
    )
    assert time.monotonic() - started < 3.0
    assert r.status == 200
    assert await r.json() == {"ok": True, "peer_notified": False}
    assert await fed_repo.get_instance("peer-u2") is None
    assert [e.instance_id for e in seen] == ["peer-u2"]


# ─── PATCH /api/pairing/connections/{instance_id} gfs_relay (v_54) ──────────


class _SwitchCalls:
    """Records what the GFS fallback switch hands the wire."""

    def __init__(self) -> None:
        self.keywrap_to: list[str] = []
        self.probed: list[str] = []

    async def send_keywrap(self, instance_id: str) -> bool:
        self.keywrap_to.append(instance_id)
        return True

    async def probe(self, instance_id: str, **_kw) -> int:
        self.probed.append(instance_id)
        return 0


def _record_switch(client) -> _SwitchCalls:
    calls = _SwitchCalls()
    svc = client.app[peer_gfs_relay_service_key]
    svc._send_capabilities = calls.send_keywrap
    svc._probe_peer = calls.probe
    return calls


async def _seed_peer(client, iid: str, **overrides) -> RemoteInstance:
    inst = dataclasses.replace(_fake_instance(iid), **overrides)
    repo = client.app[federation_repo_key]
    await repo.save_instance(inst)
    if "proto_version" in overrides:
        await repo.set_proto_version(iid, overrides["proto_version"])
    if overrides.get("gfs_relay"):
        await repo.set_gfs_relay(iid, enabled=True)
    return inst


async def _patch_relay(client, iid: str, value, tok: str | None = None):
    return await client.patch(
        f"/api/pairing/connections/{iid}",
        json={"gfs_relay": value},
        headers=_auth(tok or client._tok),
    )


async def test_patch_gfs_relay_on_opts_in_sends_key_and_probes(client):
    await _seed_peer(client, "peer-gr-1", proto_version=54)
    calls = _record_switch(client)

    r = await _patch_relay(client, "peer-gr-1", True)

    assert r.status == 200
    body = await r.json()
    assert body["gfs_relay"] is True
    assert body["gfs_routes"] == 0
    assert body["peer_keywrap_known"] is False
    assert body["gfs_relay_available"] is True
    row = await client.app[federation_repo_key].get_instance("peer-gr-1")
    assert row.gfs_relay is True
    assert calls.keywrap_to == ["peer-gr-1"]
    assert calls.probed == ["peer-gr-1"]


async def test_patch_gfs_relay_on_sends_our_keywrap_key_in_capabilities(
    client, monkeypatch
):
    """The real key hand-over: our capabilities announcement to that one
    peer carries our key-wrap key, its binding signature and suite tag."""
    await _seed_peer(client, "peer-gr-key", proto_version=54)
    sent: list[dict] = []

    class _Wire:
        _own_instance_id = "self-instance"

        async def send_event(self, *, to_instance_id, event_type, payload, **_kw):
            sent.append({"to": to_instance_id, "type": event_type, "payload": payload})

    monkeypatch.setattr(client.app[capabilities_outbound_key], "_federation", _Wire())

    r = await _patch_relay(client, "peer-gr-key", True)

    assert r.status == 200
    caps = [
        m
        for m in sent
        if m["type"] is FederationEventType.INSTANCE_CAPABILITIES_UPDATED
    ]
    assert [m["to"] for m in caps] == ["peer-gr-key"]
    payload = caps[0]["payload"]
    assert payload["keywrap_pk"] == client.app[instance_keywrap_public_key_key].hex()
    assert payload["keywrap_suite"] == "x25519"
    assert payload["keywrap_sig"]


async def test_patch_gfs_relay_off_drops_routes_and_opt_in(client):
    await _connect_gfs(client, "gfs-gr", "https://gfs-gr.example")
    await _seed_peer(client, "peer-gr-2", proto_version=54, gfs_relay=True)
    repo = client.app[federation_repo_key]
    await repo.upsert_gfs_route("peer-gr-2", "gfs-gr", now="2026-10-08T00:00:00+00:00")
    calls = _record_switch(client)

    listed = await client.get("/api/connections", headers=_auth(client._tok))
    assert [c["gfs_routes"] for c in await listed.json()] == [1]

    r = await _patch_relay(client, "peer-gr-2", False)

    assert r.status == 200
    body = await r.json()
    assert (body["gfs_relay"], body["gfs_routes"]) == (False, 0)
    assert (await repo.get_instance("peer-gr-2")).gfs_relay is False
    assert await repo.list_gfs_routes("peer-gr-2") == []
    # Off tells the peer (``gfs_relay: false``) and probes nothing.
    assert calls.keywrap_to == ["peer-gr-2"] and calls.probed == []


async def test_patch_gfs_relay_requires_admin(client):
    await _seed_peer(client, "peer-gr-3", proto_version=54)
    calls = _record_switch(client)
    member_tok = await _seed_member(client._db, "bob-gr")

    r = await _patch_relay(client, "peer-gr-3", True, tok=member_tok)

    assert r.status == 403
    assert (
        await client.app[federation_repo_key].get_instance("peer-gr-3")
    ).gfs_relay is False
    assert calls.keywrap_to == [] and calls.probed == []


async def test_patch_gfs_relay_refused_for_a_link_joined_household(client):
    await _seed_peer(
        client,
        "peer-gr-4",
        proto_version=54,
        source=InstanceSource.SPACE_SESSION,
        remote_inbox_url="",
    )
    calls = _record_switch(client)

    r = await _patch_relay(client, "peer-gr-4", True)

    assert r.status == 409
    assert (await r.json())["error"]["code"] == "GFS_RELAY_NOT_ALLOWED"
    assert calls.keywrap_to == [] and calls.probed == []


async def test_patch_gfs_relay_refused_while_pairing_is_pending(client):
    await _seed_peer(client, "peer-gr-5", status=PairingStatus.PENDING_SENT)

    r = await _patch_relay(client, "peer-gr-5", True)

    assert r.status == 409
    assert (await r.json())["error"]["code"] == "GFS_RELAY_NOT_ALLOWED"


async def test_patch_gfs_relay_unknown_peer_is_404(client):
    r = await _patch_relay(client, "nobody-here", True)

    assert r.status == 404
    assert (await r.json())["error"]["code"] == "NOT_FOUND"


async def test_patch_gfs_relay_non_bool_is_422(client):
    await _seed_peer(client, "peer-gr-6", proto_version=54)

    r = await _patch_relay(client, "peer-gr-6", "yes")

    assert r.status == 422


async def test_get_connections_reports_gfs_fallback_availability(client):
    """``gfs_relay_available`` is what the SPA's "needs a newer Social
    Home" line reads: a v_53 peer without a key can never send one; a
    v_54 peer can; a v_53 peer whose key we hold (a GFS-reach pairing)
    still works."""
    await _seed_peer(client, "peer-old", proto_version=53)
    await _seed_peer(client, "peer-new", proto_version=54)
    await _seed_peer(
        client, "peer-keyed", proto_version=53, remote_keywrap_pk="ab" * 32
    )
    await _seed_peer(client, "peer-ancient", proto_version=52)

    r = await client.get("/api/connections", headers=_auth(client._tok))

    rows = {c["instance_id"]: c for c in await r.json()}
    assert rows["peer-old"]["gfs_relay_available"] is False
    assert rows["peer-new"]["gfs_relay_available"] is True
    assert rows["peer-keyed"]["gfs_relay_available"] is True
    assert rows["peer-keyed"]["peer_keywrap_known"] is True
    assert rows["peer-ancient"]["gfs_relay_available"] is False
    for row in rows.values():
        assert row["gfs_relay"] is False
        assert row["gfs_routes"] == 0


async def _patch_both(client, iid: str, body: dict):
    return await client.patch(
        f"/api/pairing/connections/{iid}",
        json=body,
        headers=_auth(client._tok),
    )


async def test_patch_both_fields_a_bad_share_home_applies_neither(client):
    await _seed_peer(client, "peer-both-1", proto_version=54)
    calls = _record_switch(client)

    r = await _patch_both(
        client, "peer-both-1", {"share_home": "no", "gfs_relay": True}
    )

    assert r.status == 422
    row = await client.app[federation_repo_key].get_instance("peer-both-1")
    assert row.gfs_relay is False
    assert calls.keywrap_to == [] and calls.probed == []


async def test_patch_both_fields_a_bad_gfs_relay_applies_neither(client):
    await _seed_peer(client, "peer-both-2", proto_version=54)
    stub = _CapturingShareHomeSvc()
    client.app[peer_home_sharing_service_key] = stub

    r = await _patch_both(
        client, "peer-both-2", {"share_home": False, "gfs_relay": "yes"}
    )

    assert r.status == 422
    assert stub.calls == []


async def test_patch_both_fields_a_refused_switch_applies_neither(client):
    """409 for a link-joined household must not leave home sharing flipped."""
    await _seed_peer(
        client,
        "peer-both-3",
        source=InstanceSource.SPACE_SESSION,
        remote_inbox_url="",
    )
    stub = _CapturingShareHomeSvc()
    client.app[peer_home_sharing_service_key] = stub

    r = await _patch_both(
        client, "peer-both-3", {"share_home": False, "gfs_relay": True}
    )

    assert r.status == 409
    assert stub.calls == []


async def test_managing_connections_has_its_own_rate_limit_bucket(client):
    """Opening Manage spends two ``/api/pairing/connections/*`` reads and a
    switch one more; in the 5/min handshake bucket that 429'd after two
    opens and a toggle. Connection management gets 30/min of its own."""
    await _seed_peer(client, "peer-rl", proto_version=54)
    _record_switch(client)
    for _ in range(4):
        r = await client.get(
            "/api/pairing/connections/peer-rl/transport-detail",
            headers=_auth(client._tok),
        )
        assert r.status == 200
        r = await client.get(
            "/api/pairing/connections/peer-rl/visible-users",
            headers=_auth(client._tok),
        )
        assert r.status == 200
    r = await _patch_relay(client, "peer-rl", True)
    assert r.status == 200
    # …while the handshake endpoints keep the tight bucket of their own.
    statuses = [(await _initiate(client)).status for _ in range(6)]
    assert statuses[-1] == 429
