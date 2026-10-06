"""Integration tests for the GFS cluster mode (spec §24.10).

Exercises the NODE_* dispatch, ban-wins LWW, /cluster/health, and the
admin /admin/api/cluster endpoints. Uses an in-process aiohttp
:class:`TestClient` so the full HTTP signature + verification path runs.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.crypto import b64url_encode, ed25519_public_key, sign_ed25519
from socialhome.db.migrations import discover_migrations
from socialhome.global_server.admin import hash_password
from socialhome.global_server.app_keys import (
    gfs_admin_repo_key,
    gfs_cluster_key,
    gfs_cluster_repo_key,
    gfs_fed_repo_key,
)
from socialhome.global_server.cluster import (
    CLUSTER_FAILED_VERIFY_RATE_LIMIT_PER_MIN,
    CLUSTER_RATE_LIMIT_PER_MIN,
    CLUSTER_REPLAY_MAX_ENTRIES,
    CLUSTER_REPLAY_MAX_PER_NODE,
    CLUSTER_MAX_NODES,
    CLUSTER_REPLAY_SLACK_S,
    CLUSTER_REPLAY_TTL_S,
    CLUSTER_TS_SKEW_S,
    CLUSTER_UNVERIFIED_RATE_LIMIT_PER_MIN,
    NODE_HEARTBEAT,
    NODE_HELLO,
    ClusterService,
    _now_iso,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClusterNode
from socialhome.global_server.public import RATE_LIMIT_MAX_TRACKED_IPS
from socialhome.global_server.repositories import SqliteClusterRepo
from socialhome.global_server.routes import cluster as cluster_routes
from socialhome.global_server.server import SIGNING_SEED_FILENAME, create_gfs_app

_GFS_MIGRATIONS_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "socialhome/global_server/migrations"
)


def _config(tmp_dir, *, cluster=True):
    return GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id="gfs-node-a",
        cluster_enabled=cluster,
        cluster_node_id="gfs-node-a",
        cluster_peers=(),
    )


class _FrozenClock:
    """Injectable monotonic clock — the rate-limit windows never depend on
    how long the test takes to run."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def outbound_hellos(monkeypatch):
    """Stop every real outbound NODE_* POST at the boundary.

    A first-contact NODE_HELLO makes ``handle_hello`` HELLO back to the URL
    the sender advertised, and ``add_peer`` HELLOs the URL it was given —
    both used to leave the process for a real DNS lookup of a ``*.test``
    host (seconds, capped by the 5 s client timeout). Records the calls so a
    test can assert the reply was attempted.
    """
    sent: list[tuple[str, str]] = []

    async def _fake_post(self, peer_url, msg_type, payload, *, to="", session=None):
        sent.append((peer_url, msg_type))

    monkeypatch.setattr(ClusterService, "_post_to_peer", _fake_post)
    return sent


@pytest.fixture
def clock():
    return _FrozenClock()


@pytest.fixture
async def client(tmp_dir, clock, monkeypatch):
    app = create_gfs_app(_config(tmp_dir))
    monkeypatch.setattr(app[gfs_cluster_key], "_clock", clock)
    async with TestClient(TestServer(app)) as tc:
        # Seed an admin password so the admin routes accept our cookie.
        await app[gfs_admin_repo_key].set_config(
            "admin_password_hash",
            hash_password("admin-pw"),
        )
        await tc.post("/admin/login", json={"password": "admin-pw"})
        tc._app = app
        yield tc


def _keypair() -> tuple[bytes, str]:
    seed = secrets.token_bytes(32)
    return seed, ed25519_public_key(seed).hex()


def _post_node_payload(
    type_: str,
    payload: dict,
    *,
    from_node: str,
    signing_key: bytes,
    ts: object = None,
):
    body = {
        "type": type_,
        "from": from_node,
        "ts": int(time.time()) if ts is None else ts,
        # Unique per call, so two calls in the same second are two frames,
        # not a replay of one.
        "nonce": secrets.token_urlsafe(16),
        "payload": payload,
    }
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    sig = b64url_encode(sign_ed25519(signing_key, canonical))
    return canonical, sig


# ─── /cluster/health ─────────────────────────────────────────────────


async def test_cluster_health_returns_this_node(client):
    resp = await client.get("/cluster/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["node_id"] == "gfs-node-a"
    assert body["peers"] == []


# ─── NODE_HELLO (first-contact TOFU) ────────────────────────────────


async def test_node_hello_registers_peer(client, outbound_hellos):
    """A HELLO signed with this GFS's own identity seed (the shared seed the
    operator handed the node) registers the peer and is answered."""
    svc = client._app[gfs_cluster_key]
    seed = svc._signing_key
    canonical, sig = _post_node_payload(
        NODE_HELLO,
        {
            "node_id": "gfs-node-b",
            "url": "http://b.test",
            "public_key": svc.own_public_key_hex,
        },
        from_node="gfs-node-b",
        signing_key=seed,
    )
    resp = await client.post(
        "/cluster/sync",
        data=canonical,
        headers={"Content-Type": "application/json", "X-Node-Signature": sig},
    )
    assert resp.status == 200
    nodes = await client._app[gfs_cluster_repo_key].list_nodes()
    (row,) = [n for n in nodes if n.node_id == "gfs-node-b"]
    assert row.public_key == svc.own_public_key_hex
    # First contact → we HELLO back (stopped at the boundary, never sent).
    assert outbound_hellos == [("http://b.test", NODE_HELLO)]


@pytest.mark.security
async def test_node_hello_under_an_unknown_key_is_refused(client, outbound_hellos):
    """A self-signed HELLO under a key nobody approved writes nothing."""
    seed, pub_hex = _keypair()
    resp = await _sync(
        client,
        from_node="gfs-node-x",
        seed=seed,
        ip=ATTACKER_IP,
        type_=NODE_HELLO,
        payload={
            "node_id": "gfs-node-x",
            "url": "http://x.test",
            "public_key": pub_hex,
        },
    )
    assert resp.status == 403
    assert (await resp.json())["error"] == "unapproved_node"
    assert await client._app[gfs_cluster_repo_key].list_nodes() == []
    assert outbound_hellos == []
    assert ATTACKER_IP in client._app[gfs_cluster_key]._sync_unverified_limiter


@pytest.mark.security
async def test_node_hello_with_a_different_key_is_a_mismatch(client, caplog):
    """A known node id announcing a key other than its pin is refused, the
    pin is untouched, and the operator sees a WARNING."""
    pinned_seed = await _register_peer(client)
    pinned_hex = ed25519_public_key(pinned_seed).hex()
    seed, pub_hex = _keypair()
    with caplog.at_level("WARNING"):
        resp = await _sync(
            client,
            from_node=PEER,
            seed=seed,
            ip=ATTACKER_IP,
            type_=NODE_HELLO,
            payload={"node_id": PEER, "url": "http://evil.test", "public_key": pub_hex},
        )
    assert resp.status == 403
    assert (await resp.json())["error"] == "key_mismatch"
    (row,) = await client._app[gfs_cluster_repo_key].list_nodes()
    assert row.approved_key == pinned_hex
    assert row.url == f"http://{PEER}.test"
    assert any(
        r.levelname == "WARNING" and "key_mismatch" in r.getMessage()
        for r in caplog.records
    )
    svc = client._app[gfs_cluster_key]
    assert PEER not in svc._sync_node_limiter


async def test_self_hello_is_a_no_op(client, outbound_hellos):
    """The Nomad template lists this node too, so it HELLOs itself."""
    svc = client._app[gfs_cluster_key]
    resp = await _sync(
        client,
        from_node="gfs-node-a",
        seed=svc._signing_key,
        ip=GENUINE_IP,
        type_=NODE_HELLO,
        payload={
            "node_id": "gfs-node-a",
            "url": "http://gfs.test",
            "public_key": svc.own_public_key_hex,
        },
    )
    assert resp.status == 200
    assert await client._app[gfs_cluster_repo_key].list_nodes() == []
    assert outbound_hellos == []


async def test_cluster_sync_unknown_node_is_403(client):
    """An unregistered peer can only send NODE_HELLO (TOFU); anything
    else gets 403 without signature verification.
    """
    canonical = json.dumps(
        {
            "type": NODE_HEARTBEAT,
            "from": "ghost",
            "ts": int(time.time()),
            "payload": {},
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    resp = await client.post(
        "/cluster/sync",
        data=canonical,
        headers={
            "Content-Type": "application/json",
            "X-Node-Signature": "sig",
            "X-Node-Id": "ghost",
        },
    )
    assert resp.status == 403


# ─── NODE_SYNC_CLIENT / SPACE apply via app.ClusterService ──────────


async def test_apply_sync_client_upserts(client):
    svc = client._app[gfs_cluster_key]
    await svc.apply_sync_client(
        action="upsert",
        client_instance={
            "instance_id": "peer.home",
            "display_name": "Peer",
            "public_key": "aa" * 32,
            "status": "active",
        },
    )
    fed_repo = client._app[gfs_fed_repo_key]
    inst = await fed_repo.get_instance("peer.home")
    assert inst is not None
    assert inst.status == "active"


async def test_apply_sync_space_banned_wins_lww(client):
    """A ban upsert must never be overwritten by a later non-ban upsert."""
    svc = client._app[gfs_cluster_key]
    fed_repo = client._app[gfs_fed_repo_key]
    # Seed an owner (FK).
    await svc.apply_sync_client(
        action="upsert",
        client_instance={
            "instance_id": "owner.home",
            "display_name": "O",
            "public_key": "bb" * 32,
            "status": "active",
        },
    )
    # Ban the space.
    await svc.apply_sync_space(
        action="ban",
        global_space={
            "space_id": "lww-space",
            "owning_instance": "owner.home",
            "name": "Banned Space",
            "status": "banned",
        },
    )
    # Later "active" upsert must be ignored.
    await svc.apply_sync_space(
        action="upsert",
        global_space={
            "space_id": "lww-space",
            "owning_instance": "owner.home",
            "name": "Innocent Space",
            "status": "active",
        },
    )
    sp = await fed_repo.get_space("lww-space")
    assert sp.status == "banned"


# ─── NODE_POLICY_PUSH ─────────────────────────────────────────────────


async def test_apply_policy_push_updates_server_config(client):
    svc = client._app[gfs_cluster_key]
    await svc.apply_policy_push(
        {
            "auto_accept_clients": "0",
            "fraud_threshold": "7",
        }
    )
    admin_repo = client._app[gfs_admin_repo_key]
    assert await admin_repo.get_config("auto_accept_clients") == "0"
    assert await admin_repo.get_config("fraud_threshold") == "7"


# ─── Phase Z: NODE_SYNC_REPORT ────────────────────────────────────────


async def test_apply_sync_report_persists_idempotent(client):
    svc = client._app[gfs_cluster_key]
    admin_repo = client._app[gfs_admin_repo_key]
    report = {
        "id": "rpt-xyz",
        "target_type": "space",
        "target_id": "sp-foo",
        "category": "spam",
        "notes": None,
        "reporter_instance_id": "reporter.home",
        "reporter_user_id": None,
        "status": "pending",
        "created_at": 1700000000,
    }
    await svc.apply_sync_report(report)
    # Second apply is a no-op (UNIQUE index on reporter+target).
    await svc.apply_sync_report(report)
    rows = await admin_repo.list_fraud_reports(status="pending")
    assert len(rows) == 1
    assert rows[0].reporter_instance_id == "reporter.home"


# ─── Admin cluster tab ────────────────────────────────────────────────


async def test_admin_cluster_list_returns_health(client):
    resp = await client.get("/admin/api/cluster")
    assert resp.status == 200
    body = await resp.json()
    assert body["node_id"] == "gfs-node-a"


async def test_admin_cluster_add_and_remove_peer(client, outbound_hellos):
    from urllib.parse import quote

    _seed, pub_hex = _keypair()
    resp = await client.post(
        "/admin/api/cluster/peers",
        json={
            "node_id": "gfs-node-c",
            "url": "http://peer-c.test/",
            "public_key": pub_hex.upper(),
        },
    )
    assert resp.status == 201
    body = await resp.json()
    assert body == {
        "node_id": "gfs-node-c",
        "url": "http://peer-c.test",
        "public_key": pub_hex,
    }
    cluster_repo = client._app[gfs_cluster_repo_key]
    (row,) = await cluster_repo.list_nodes()
    assert (row.node_id, row.url, row.public_key) == (
        "gfs-node-c",
        "http://peer-c.test",
        pub_hex,
    )
    assert row.last_seen is None
    # We HELLO the new peer so it learns us too.
    assert outbound_hellos == [("http://peer-c.test", NODE_HELLO)]
    resp = await client.delete(
        f"/admin/api/cluster/peers/{quote(body['node_id'], safe='')}",
    )
    assert resp.status == 200
    assert await cluster_repo.list_nodes() == []


_VALID_KEY = ed25519_public_key(b"\x01" * 32).hex()


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"url": "http://c.test", "public_key": _VALID_KEY}, "invalid_node_id"),
        (
            {"node_id": "  ", "url": "http://c.test", "public_key": _VALID_KEY},
            "invalid_node_id",
        ),
        (
            {"node_id": 7, "url": "http://c.test", "public_key": _VALID_KEY},
            "invalid_node_id",
        ),
        (
            {"node_id": "x" * 129, "url": "http://c.test", "public_key": _VALID_KEY},
            "invalid_node_id",
        ),
        (
            {"node_id": "gfs-node-a", "url": "http://c.test", "public_key": _VALID_KEY},
            "node_id_is_self",
        ),
        ({"node_id": "c", "public_key": _VALID_KEY}, "invalid_url"),
        (
            {"node_id": "c", "url": "ftp://c.test", "public_key": _VALID_KEY},
            "invalid_url",
        ),
        ({"node_id": "c", "url": "http://", "public_key": _VALID_KEY}, "invalid_url"),
        (
            {"node_id": "c", "url": "http://u:p@c.test", "public_key": _VALID_KEY},
            "invalid_url",
        ),
        (
            {"node_id": "c", "url": "http://c.test/?x=1", "public_key": _VALID_KEY},
            "invalid_url",
        ),
        (
            {"node_id": "c", "url": "http://c.test/#f", "public_key": _VALID_KEY},
            "invalid_url",
        ),
        ({"node_id": "c", "url": "http://c.test"}, "invalid_public_key"),
        (
            {"node_id": "c", "url": "http://c.test", "public_key": "ab" * 31},
            "invalid_public_key",
        ),
        (
            {"node_id": "c", "url": "http://c.test", "public_key": "zz" * 32},
            "invalid_public_key",
        ),
        (
            {"node_id": "c", "url": "http://c.test", "public_key": _VALID_KEY + "00"},
            "invalid_public_key",
        ),
    ],
)
async def test_admin_cluster_add_peer_rejects_bad_input(client, body, error):
    resp = await client.post("/admin/api/cluster/peers", json=body)
    assert resp.status == 422
    assert (await resp.json())["error"] == error
    assert await client._app[gfs_cluster_repo_key].list_nodes() == []


@pytest.mark.parametrize(
    "node_id",
    [
        "a\x00b",
        "a\u202eb",  # RIGHT-TO-LEFT OVERRIDE
        "a\u200bb",  # zero-width space
        "a b",
        "a\nb",
        "nöde",
        "a\tb",
    ],
)
async def test_admin_cluster_add_peer_rejects_unsafe_node_id_characters(
    client, node_id
):
    resp = await client.post(
        "/admin/api/cluster/peers",
        json={"node_id": node_id, "url": "http://c.test", "public_key": _VALID_KEY},
    )
    assert (resp.status, await resp.json()) == (422, {"error": "invalid_node_id"})


@pytest.mark.parametrize(
    "node_id",
    [
        "gfs-node-0",
        "3f2a9c1e-7b4d-4e8f-9a6b-1c2d3e4f5a6b",
        "gfs_1.eu:8765",
        "https://gfs-1.example.com:8443",
    ],
)
async def test_admin_cluster_add_peer_accepts_existing_node_id_shapes(client, node_id):
    seed, pub_hex = _keypair()
    resp = await client.post(
        "/admin/api/cluster/peers",
        json={"node_id": node_id, "url": "http://c.test", "public_key": pub_hex},
    )
    assert resp.status == 201
    assert (await resp.json())["node_id"] == node_id


@pytest.mark.parametrize("payload", [[], 0, "x", None, [{"a": 1}], True])
async def test_non_object_payload_is_malformed(client, payload):
    seed = await _register_peer(client)
    canonical, sig = _sign_body(
        {
            "type": NODE_HEARTBEAT,
            "from": PEER,
            "ts": int(time.time()),
            "nonce": secrets.token_urlsafe(16),
            "payload": payload,
        },
        seed,
    )
    resp = await _post_raw(client, canonical, sig, GENUINE_IP)
    assert (resp.status, await resp.json()) == (400, {"error": "invalid_message"})


@pytest.mark.security
async def test_admin_cluster_add_peer_never_moves_a_pin(client):
    """Re-adding a node id under a different key is a 409; rotation is
    delete then re-add. Re-adding the same key is idempotent."""
    peer_seed = await _register_peer(client)
    pinned = ed25519_public_key(peer_seed).hex()
    resp = await client.post(
        "/admin/api/cluster/peers",
        json={"node_id": PEER, "url": "http://b.test", "public_key": _VALID_KEY},
    )
    assert resp.status == 409
    assert (await resp.json())["error"] == "key_mismatch"
    (row,) = await client._app[gfs_cluster_repo_key].list_nodes()
    assert row.approved_key == pinned
    resp = await client.post(
        "/admin/api/cluster/peers",
        json={"node_id": PEER, "url": "http://b2.test", "public_key": pinned},
    )
    assert resp.status == 201
    (row,) = await client._app[gfs_cluster_repo_key].list_nodes()
    assert (row.url, row.approved_key, row.status) == (
        "http://b2.test",
        pinned,
        "online",
    )


async def test_admin_added_peer_is_answered_on_its_first_hello(client, outbound_hellos):
    """An admin-added row has never been seen (``last_seen`` is None), so
    the node's first HELLO is answered — the two sides converge whichever
    was added first."""
    seed, pub_hex = _keypair()
    resp = await client.post(
        "/admin/api/cluster/peers",
        json={"node_id": "gfs-node-c", "url": "http://c.test", "public_key": pub_hex},
    )
    assert resp.status == 201
    outbound_hellos.clear()
    hello = {"node_id": "gfs-node-c", "url": "http://c.test", "public_key": pub_hex}
    resp = await _sync(
        client,
        from_node="gfs-node-c",
        seed=seed,
        ip=GENUINE_IP,
        type_=NODE_HELLO,
        payload=hello,
    )
    assert resp.status == 200
    assert outbound_hellos == [("http://c.test", NODE_HELLO)]
    (row,) = await client._app[gfs_cluster_repo_key].list_nodes()
    assert row.status == "online" and row.last_seen is not None
    # Now known: a second HELLO is not answered (no ping-pong).
    outbound_hellos.clear()
    resp = await _sync(
        client,
        from_node="gfs-node-c",
        seed=seed,
        ip=GENUINE_IP,
        type_=NODE_HELLO,
        payload=hello,
    )
    assert resp.status == 200
    assert outbound_hellos == []


async def test_admin_cluster_publishes_our_own_key(client):
    """The operator needs this node's key to pin it on the other nodes."""
    resp = await client.get("/admin/api/cluster")
    body = await resp.json()
    svc = client._app[gfs_cluster_key]
    assert body["public_key"] == svc.own_public_key_hex
    assert len(body["public_key"]) == 64


async def test_admin_cluster_ping_unknown_is_404(client):
    resp = await client.post("/admin/api/cluster/peers/ghost/ping")
    assert resp.status == 404


@pytest.mark.security
async def test_admin_ping_refuses_a_row_that_is_not_a_member(client, monkeypatch):
    """A non-member row (e.g. one an old-version node wrote by TOFU) carries a
    URL nobody validated; an admin click must not send a request there."""
    pinged: list[str] = []

    async def _record(self, url):
        pinged.append(url)
        return True

    monkeypatch.setattr(ClusterService, "_ping_peer", _record)
    _seed, pub_hex = _keypair()
    await client._app[gfs_cluster_repo_key].insert_node(
        ClusterNode(
            node_id="tofu",
            url="http://169.254.169.254/latest",
            public_key=pub_hex,
            status="online",
            last_seen="2026-01-01 00:00:00",
        )
    )
    resp = await client.post("/admin/api/cluster/peers/tofu/ping")
    assert (resp.status, await resp.json()) == (409, {"error": "not_a_member"})
    assert pinged == []


async def test_admin_cluster_add_peer_missing_url_422(client):
    resp = await client.post("/admin/api/cluster/peers", json={})
    assert resp.status == 422


# ─── Single-node health (cluster disabled) ────────────────────────────


async def test_single_node_health_reports_single_node(tmp_dir):
    app = create_gfs_app(_config(tmp_dir, cluster=False))
    async with TestClient(TestServer(app)) as tc:
        resp = await tc.get("/cluster/health")
        body = await resp.json()
        assert body["status"] == "single-node"


# ─── Rate limiting on /cluster/sync ───────────────────────────────────

PEER = "gfs-node-b"
GENUINE_IP = "198.51.100.7"
ATTACKER_IP = "203.0.113.9"


async def _register_peer(client, node_id: str = PEER) -> bytes:
    """Seed a known cluster peer directly — no HELLO, no first-contact reply."""
    seed, pub_hex = _keypair()
    repo = client._app[gfs_cluster_repo_key]
    await repo.approve_node(node_id, f"http://{node_id}.test", pub_hex)
    await repo.touch_node(node_id, status="online", last_seen=None)
    return seed


async def _sync(
    client,
    *,
    from_node: str,
    seed: bytes | None,
    ip: str,
    type_: str = NODE_HEARTBEAT,
    payload: dict | None = None,
    ts: object = None,
):
    """POST one NODE_* message; ``seed=None`` sends a forged signature.

    The test client connects from loopback, a default trusted proxy, so the
    GFS believes ``X-Forwarded-For`` — that is how one test plays two hosts.
    """
    if seed is None:
        canonical = json.dumps(
            {
                "type": type_,
                "from": from_node,
                "ts": int(time.time()) if ts is None else ts,
                "nonce": secrets.token_urlsafe(16),
                "payload": {},
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        sig = b64url_encode(b"\x00" * 64)
    else:
        canonical, sig = _post_node_payload(
            type_, payload or {}, from_node=from_node, signing_key=seed, ts=ts
        )
    return await client.post(
        "/cluster/sync",
        data=canonical,
        headers={
            "Content-Type": "application/json",
            "X-Node-Signature": sig,
            "X-Node-Id": from_node,
            "X-Forwarded-For": ip,
        },
    )


@pytest.mark.security
async def test_spoofed_flood_cannot_rate_limit_a_genuine_peer(client):
    """Forged requests claiming peer X never spend X's budget.

    The limiter used to run before the signature check, keyed on the CLAIMED
    ``from`` — 60 unsigned POSTs a minute naming a real peer locked that peer's
    genuine sync out with 429.
    """
    seed = await _register_peer(client)
    for _ in range(CLUSTER_RATE_LIMIT_PER_MIN + 10):
        resp = await _sync(client, from_node=PEER, seed=None, ip=ATTACKER_IP)
        assert resp.status in (401, 429)
    resp = await _sync(client, from_node=PEER, seed=seed, ip=GENUINE_IP)
    assert resp.status == 200


@pytest.mark.security
async def test_verified_peer_over_budget_is_rate_limited(client, clock):
    """A peer that really signs more than 60 messages a minute gets 429."""
    seed = await _register_peer(client)
    for _ in range(CLUSTER_RATE_LIMIT_PER_MIN):
        resp = await _sync(client, from_node=PEER, seed=seed, ip=GENUINE_IP)
        assert resp.status == 200
    resp = await _sync(client, from_node=PEER, seed=seed, ip=GENUINE_IP)
    assert resp.status == 429
    assert resp.headers["Retry-After"] == "60"
    # The budget is per verified node, not per address: another peer on the
    # same host is unaffected.
    other = await _register_peer(client, "gfs-node-c")
    resp = await _sync(client, from_node="gfs-node-c", seed=other, ip=GENUINE_IP)
    assert resp.status == 200
    # The window slides: a minute later the peer is welcome again.
    clock.now += 61
    resp = await _sync(client, from_node=PEER, seed=seed, ip=GENUINE_IP)
    assert resp.status == 200


@pytest.fixture
def verify_calls(monkeypatch):
    """Count every signature verification the sync route performs."""
    calls: list[str] = []
    real = cluster_routes.verify_node_signature

    def _counting(raw, sig, key):
        calls.append(key)
        return real(raw, sig, key)

    monkeypatch.setattr(cluster_routes, "verify_node_signature", _counting)
    return calls


@pytest.mark.security
async def test_unverified_flood_hits_the_per_address_bound(client, clock, verify_calls):
    """Once an address has spent its unverified budget, every frame that
    does not name an approved node is shed with 429 — no signature work."""
    await _register_peer(client)
    for _ in range(CLUSTER_UNVERIFIED_RATE_LIMIT_PER_MIN):
        resp = await _sync(client, from_node="ghost", seed=None, ip=ATTACKER_IP)
        assert resp.status == 403
    for bad in (b"not json", b"[]", b'{"type": "x"}'):
        resp = await client.post(
            "/cluster/sync", data=bad, headers={"X-Forwarded-For": ATTACKER_IP}
        )
        assert resp.status == 429
    for node in ("ghost", "ghost-2"):
        resp = await _sync(client, from_node=node, seed=None, ip=ATTACKER_IP)
        assert resp.status == 429
    seed, pub = _keypair()
    resp = await _sync(
        client,
        from_node="sybil",
        seed=seed,
        ip=ATTACKER_IP,
        type_=NODE_HELLO,
        payload={"node_id": "sybil", "url": "", "public_key": pub},
    )
    assert resp.status == 429
    assert verify_calls == []
    # Another address still gets a real answer.
    resp = await _sync(client, from_node="ghost", seed=None, ip=GENUINE_IP)
    assert resp.status == 403
    clock.now += 61
    resp = await _sync(client, from_node="ghost", seed=None, ip=ATTACKER_IP)
    assert resp.status == 403


@pytest.mark.security
async def test_junk_from_a_peers_address_cannot_lock_the_peer_out(client):
    """Junk (malformed, unknown senders, stale) from the address a member
    syncs from spends that address's budget — but a frame that verifies
    under the member's pin is still accepted once the address is shed."""
    seed = await _register_peer(client)
    for i in range(CLUSTER_UNVERIFIED_RATE_LIMIT_PER_MIN + 1):
        resp = await client.post(
            "/cluster/sync",
            data=b"not json" if i % 2 else b'{"from": "x"}',
            headers={"X-Forwarded-For": GENUINE_IP},
        )
        assert resp.status in (400, 429)
    assert resp.status == 429
    resp = await _sync(client, from_node=PEER, seed=seed, ip=GENUINE_IP)
    assert resp.status == 200
    # …and keeps being accepted, within its own (verified) budget.
    resp = await _sync(client, from_node=PEER, seed=seed, ip=GENUINE_IP)
    assert resp.status == 200


@pytest.mark.security
async def test_forged_frames_naming_a_peer_from_a_shed_address_are_bounded(
    client, clock, verify_calls
):
    """From a shed address, frames naming an approved node are verified
    (so the real node gets through) but each FAILED verify spends a small
    per-(node, address) budget; once spent, more forgeries from that
    address are shed without a verify.
    The node's own verified budget is never touched."""
    seed = await _register_peer(client)
    for _ in range(CLUSTER_UNVERIFIED_RATE_LIMIT_PER_MIN):
        await client.post(
            "/cluster/sync", data=b"junk", headers={"X-Forwarded-For": ATTACKER_IP}
        )
    verify_calls.clear()
    statuses = [
        (await _sync(client, from_node=PEER, seed=None, ip=ATTACKER_IP)).status
        for _ in range(CLUSTER_FAILED_VERIFY_RATE_LIMIT_PER_MIN + 20)
    ]
    # Every rejection from a shed address is a 429; only the first 30 cost
    # a verify.
    assert statuses == [429] * (CLUSTER_FAILED_VERIFY_RATE_LIMIT_PER_MIN + 20)
    assert len(verify_calls) == CLUSTER_FAILED_VERIFY_RATE_LIMIT_PER_MIN
    svc = client._app[gfs_cluster_key]
    assert PEER not in svc._sync_node_limiter
    # From an address that is NOT shed, the genuine node is unaffected.
    resp = await _sync(client, from_node=PEER, seed=seed, ip=GENUINE_IP)
    assert resp.status == 200
    # The bound is per minute.
    clock.now += 61
    resp = await _sync(client, from_node=PEER, seed=None, ip=ATTACKER_IP)
    assert resp.status == 401


@pytest.mark.security
async def test_malformed_bodies_count_against_the_address(client):
    """Bad JSON / missing fields / unknown senders are failures too."""
    bodies = [b"not json", b"[]", b'{"type": "x"}', b'{"from": "x"}']
    for i in range(CLUSTER_UNVERIFIED_RATE_LIMIT_PER_MIN):
        resp = await client.post(
            "/cluster/sync",
            data=bodies[i % len(bodies)],
            headers={"X-Forwarded-For": ATTACKER_IP},
        )
        assert resp.status == 400
    resp = await client.post(
        "/cluster/sync", data=b"{}", headers={"X-Forwarded-For": ATTACKER_IP}
    )
    assert resp.status == 429


@pytest.mark.security
async def test_unapproved_hellos_spend_the_address_budget_not_a_node_budget(
    client, outbound_hellos
):
    """HELLOs under unapproved keys are refused, write nothing and are
    charged to the source address until it is shed. A HELLO from a known
    peer under its pinned key is that peer's own (verified) traffic."""
    for i in range(CLUSTER_UNVERIFIED_RATE_LIMIT_PER_MIN):
        seed, pub_hex = _keypair()
        node = f"sybil-{i}"
        resp = await _sync(
            client,
            from_node=node,
            seed=seed,
            ip=ATTACKER_IP,
            type_=NODE_HELLO,
            payload={"node_id": node, "url": "", "public_key": pub_hex},
        )
        assert resp.status == 403
    seed, pub_hex = _keypair()
    resp = await _sync(
        client,
        from_node="sybil-x",
        seed=seed,
        ip=ATTACKER_IP,
        type_=NODE_HELLO,
        payload={"node_id": "sybil-x", "url": "", "public_key": pub_hex},
    )
    assert resp.status == 429
    svc = client._app[gfs_cluster_key]
    assert len(svc._sync_node_limiter) == 0
    assert await client._app[gfs_cluster_repo_key].list_nodes() == []
    assert outbound_hellos == []
    peer_seed = await _register_peer(client)
    resp = await _sync(
        client,
        from_node=PEER,
        seed=peer_seed,
        ip=GENUINE_IP,
        type_=NODE_HELLO,
        payload={
            "node_id": PEER,
            "url": f"http://{PEER}.test",
            "public_key": ed25519_public_key(peer_seed).hex(),
        },
    )
    assert resp.status == 200
    assert PEER in svc._sync_node_limiter
    assert GENUINE_IP not in svc._sync_unverified_limiter


@pytest.mark.security
async def test_random_sender_ids_do_not_grow_the_limiter(client):
    """Random ``from`` values used to add one never-pruned key each."""
    for i in range(500):
        await _sync(
            client,
            from_node=secrets.token_hex(8),
            seed=None,
            ip=f"203.0.{i // 256}.{i % 256}",
        )
    svc = client._app[gfs_cluster_key]
    # Unverified senders never reach the per-node limiter …
    assert len(svc._sync_node_limiter) == 0
    # … and the per-address limiter holds one bucket per source address.
    assert len(svc._sync_unverified_limiter) == 500


async def test_cluster_limiters_are_capped_lrus(gfs_db):
    """Both cluster limiters carry the shared key cap, so no sender — real or
    forged, one address or a botnet — can grow them without bound."""
    svc = ClusterService(SqliteClusterRepo(gfs_db))
    for limiter in (svc._sync_node_limiter, svc._sync_unverified_limiter):
        for i in range(RATE_LIMIT_MAX_TRACKED_IPS + 500):
            limiter.allow(f"k{i}", now=1000.0)
        assert len(limiter) == RATE_LIMIT_MAX_TRACKED_IPS


@pytest.mark.security
async def test_sender_id_comes_only_from_the_signed_body(client):
    """``from`` is inside the signed body; the unsigned ``X-Node-Id`` header
    must never stand in for it — a body without ``from`` is malformed."""
    seed = await _register_peer(client)
    body = {"type": NODE_HEARTBEAT, "ts": int(time.time()), "payload": {}}
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    resp = await client.post(
        "/cluster/sync",
        data=canonical,
        headers={
            "Content-Type": "application/json",
            "X-Node-Signature": b64url_encode(sign_ed25519(seed, canonical)),
            "X-Node-Id": PEER,
            "X-Forwarded-For": GENUINE_IP,
        },
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_message"


# ─── Timestamp window ─────────────────────────────────────────────────


@pytest.mark.security
@pytest.mark.parametrize("offset", [-(CLUSTER_TS_SKEW_S + 1), CLUSTER_TS_SKEW_S + 1])
async def test_stale_or_future_timestamp_is_refused(client, offset):
    """A frame signed more than ±300 s from our wall clock is refused before
    the signature is checked, and charged to the address, not the node."""
    seed = await _register_peer(client)
    resp = await _sync(
        client,
        from_node=PEER,
        seed=seed,
        ip=ATTACKER_IP,
        ts=int(time.time()) + offset,
    )
    assert resp.status == 401
    assert (await resp.json())["error"] == "stale_timestamp"
    svc = client._app[gfs_cluster_key]
    assert ATTACKER_IP in svc._sync_unverified_limiter
    assert PEER not in svc._sync_node_limiter


@pytest.mark.security
@pytest.mark.parametrize("bad_ts", [True, "1700000000", 1.5e9, None])
async def test_non_integer_timestamp_is_malformed(client, bad_ts):
    seed = await _register_peer(client)
    canonical, sig = _post_node_payload(
        NODE_HEARTBEAT, {}, from_node=PEER, signing_key=seed, ts=bad_ts
    )
    if bad_ts is None:
        # Drop the field entirely.
        body = json.loads(canonical)
        body.pop("ts")
        canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
        sig = b64url_encode(sign_ed25519(seed, canonical))
    resp = await client.post(
        "/cluster/sync",
        data=canonical,
        headers={"X-Node-Signature": sig, "X-Forwarded-For": ATTACKER_IP},
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_timestamp"
    assert ATTACKER_IP in client._app[gfs_cluster_key]._sync_unverified_limiter


async def test_timestamp_window_follows_the_injected_wall_clock(client):
    """The window reads the injected wall clock, never ``time.time`` direct."""
    seed = await _register_peer(client)
    svc = client._app[gfs_cluster_key]
    svc._wall_clock = lambda: 2_000_000_000.0
    resp = await _sync(
        client, from_node=PEER, seed=seed, ip=GENUINE_IP, ts=2_000_000_000 - 299
    )
    assert resp.status == 200
    resp = await _sync(client, from_node=PEER, seed=seed, ip=GENUINE_IP)
    assert resp.status == 401


# ─── Replay cache ─────────────────────────────────────────────────────


async def _post_raw(client, canonical: bytes, sig: str, ip: str):
    return await client.post(
        "/cluster/sync",
        data=canonical,
        headers={"X-Node-Signature": sig, "X-Forwarded-For": ip},
    )


@pytest.mark.security
async def test_replayed_frame_is_refused_and_charged_to_the_address(client):
    """Byte-identical signed frames are dispatched once. The replay is
    charged to the replaying address — a replay proves nothing about who
    sent it, so it must never spend the genuine node's budget."""
    seed = await _register_peer(client)
    canonical, sig = _post_node_payload(
        NODE_HEARTBEAT, {}, from_node=PEER, signing_key=seed
    )
    resp = await _post_raw(client, canonical, sig, GENUINE_IP)
    assert resp.status == 200
    svc = client._app[gfs_cluster_key]
    assert svc._sync_node_limiter.exhausted(PEER) is False
    for _ in range(3):
        resp = await _post_raw(client, canonical, sig, ATTACKER_IP)
        assert resp.status == 409
        assert (await resp.json())["error"] == "replay"
    assert ATTACKER_IP in svc._sync_unverified_limiter
    # The node budget was spent once — by the genuine delivery.
    assert len(svc._sync_node_limiter._hits[PEER]) == 1


@pytest.mark.security
async def test_a_rejected_frame_does_not_poison_the_replay_cache(client):
    """The digest is recorded only once a frame is accepted, so bytes that
    failed (here: over budget) are not later refused as a replay."""
    seed = await _register_peer(client)
    svc = client._app[gfs_cluster_key]
    for _ in range(CLUSTER_RATE_LIMIT_PER_MIN):
        svc.charge_verified_sync(PEER)
    canonical, sig = _post_node_payload(
        NODE_HEARTBEAT, {}, from_node=PEER, signing_key=seed
    )
    resp = await _post_raw(client, canonical, sig, GENUINE_IP)
    assert resp.status == 429
    assert len(svc._seen_frames) == 0


@pytest.mark.security
async def test_frames_signed_before_this_process_started_are_refused(client):
    """Boot floor: the replay cache is empty after a restart, so a frame
    signed before this process started (still inside ±300 s) is refused."""
    seed = await _register_peer(client)
    resp = await _sync(
        client,
        from_node=PEER,
        seed=seed,
        ip=GENUINE_IP,
        ts=int(time.time()) - 60,
    )
    assert resp.status == 401
    assert (await resp.json())["error"] == "stale_timestamp"


async def test_replay_cache_is_sized_from_the_cluster_constants(gfs_db):
    """The cache must outlive the ``ts`` window (else a frame replays after
    expiring but while still fresh), give each node room for every frame
    its verified budget can admit within the TTL, and never refuse a node
    for want of total room (else an honest frame from a large approved
    roster is refused)."""
    assert CLUSTER_REPLAY_TTL_S >= 2 * CLUSTER_TS_SKEW_S + CLUSTER_REPLAY_SLACK_S
    assert CLUSTER_REPLAY_MAX_PER_NODE >= CLUSTER_RATE_LIMIT_PER_MIN * (
        CLUSTER_REPLAY_TTL_S / 60
    )
    # No total cap: the per-node share is the binding bound (only members'
    # verified frames are recorded), so a large approved roster is never
    # refused for want of room.
    assert CLUSTER_REPLAY_MAX_ENTRIES is None
    svc = ClusterService(SqliteClusterRepo(gfs_db))
    assert svc._seen_frames._cap == CLUSTER_REPLAY_MAX_ENTRIES
    assert svc._seen_frames._per_node_cap == CLUSTER_REPLAY_MAX_PER_NODE


# ─── sig_suite + old-sender compatibility ─────────────────────────────


def _sign_body(body: dict, seed: bytes) -> tuple[bytes, str]:
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return canonical, b64url_encode(sign_ed25519(seed, canonical))


@pytest.mark.security
@pytest.mark.parametrize("suite", ["ed25519+mldsa65", "ED25519", "", 7])
async def test_unknown_sig_suite_is_refused_without_fallback(client, suite):
    """Receivers reject a suite they don't know — never fall back to
    ed25519 — and charge the address."""
    seed = await _register_peer(client)
    canonical, sig = _sign_body(
        {
            "type": NODE_HEARTBEAT,
            "from": PEER,
            "ts": int(time.time()),
            "nonce": secrets.token_urlsafe(16),
            "sig_suite": suite,
            "payload": {},
        },
        seed,
    )
    resp = await _post_raw(client, canonical, sig, ATTACKER_IP)
    assert resp.status == 400
    assert (await resp.json())["error"] == "unsupported_sig_suite"
    assert ATTACKER_IP in client._app[gfs_cluster_key]._sync_unverified_limiter


@pytest.mark.security
async def test_current_frame_with_ed25519_suite_is_accepted(client):
    seed = await _register_peer(client)
    canonical, sig = _sign_body(
        {
            "type": NODE_HEARTBEAT,
            "from": PEER,
            "ts": int(time.time()),
            "nonce": secrets.token_urlsafe(16),
            "sig_suite": "ed25519",
            "payload": {},
        },
        seed,
    )
    resp = await _post_raw(client, canonical, sig, GENUINE_IP)
    assert resp.status == 200


@pytest.mark.security
async def test_old_shape_frame_without_nonce_or_suite_still_syncs(client):
    """A node on the previous release signs ``{type, from, ts, payload}``
    with no ``nonce`` and no ``sig_suite``; a missing suite means ed25519,
    so a mixed-version cluster keeps syncing during a rolling upgrade."""
    seed = await _register_peer(client)
    canonical, sig = _sign_body(
        {"type": NODE_HEARTBEAT, "from": PEER, "ts": int(time.time()), "payload": {}},
        seed,
    )
    resp = await _post_raw(client, canonical, sig, GENUINE_IP)
    assert resp.status == 200
    # Without a nonce, a resend of the same bytes is still a replay.
    resp = await _post_raw(client, canonical, sig, GENUINE_IP)
    assert resp.status == 409


# ─── Upgrade compatibility ────────────────────────────────────────────


def _pre_upgrade_gfs(tmp_dir, own_seed: bytes, rows: list[tuple[str, str]]) -> None:
    """A GFS data dir as the previous release left it: migrated through
    0016, holding *rows* ``(node_id, public_key)`` and our identity seed."""
    (tmp_dir / SIGNING_SEED_FILENAME).write_bytes(own_seed)
    conn = sqlite3.connect(tmp_dir / "gfs.db", isolation_level=None)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            " version INTEGER PRIMARY KEY, description TEXT,"
            " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        for mig in discover_migrations(_GFS_MIGRATIONS_DIR):
            if mig.version > 16:
                break
            mig.apply(conn)
            conn.execute(
                "INSERT INTO schema_version(version, description) VALUES (?,?)",
                (mig.version, mig.description),
            )
        for node_id, key in rows:
            conn.execute(
                "INSERT INTO cluster_nodes(node_id, url, public_key, status,"
                " last_seen) VALUES(?, ?, ?, 'online', '2026-01-01 00:00:00')",
                (node_id, f"http://{node_id}.test", key),
            )
    finally:
        conn.close()


@pytest.mark.security
async def test_upgrade_trusts_no_pre_existing_key_and_shared_seed_siblings_stay(
    tmp_dir, outbound_hellos
):
    """Keys stored before operator approval may be TOFU intruders or legacy
    DERIVED keys anyone can compute. The upgrade adds ``approved_key`` empty
    for every row and keeps ``public_key`` untrusted: a shared-seed sibling
    stays a member through our own key, while a distinct-key peer — and a
    forger holding a derived key — must be re-added by an admin."""
    own_seed = secrets.token_bytes(32)
    own_key = ed25519_public_key(own_seed).hex()
    foreign_seed, foreign_key = _keypair()
    derived_seed = hashlib.sha256(b"gfs-cluster-legacy-node").digest()
    derived_key = ed25519_public_key(derived_seed).hex()
    _pre_upgrade_gfs(
        tmp_dir,
        own_seed,
        [("sibling", own_key), ("old-peer", foreign_key), ("legacy-node", derived_key)],
    )
    app = create_gfs_app(_config(tmp_dir))
    async with TestClient(TestServer(app)) as tc:
        repo = app[gfs_cluster_repo_key]
        rows = {n.node_id: n for n in await repo.list_nodes()}
        assert {k: (r.public_key, r.approved_key) for k, r in rows.items()} == {
            "sibling": (own_key, ""),
            "old-peer": (foreign_key, ""),
            "legacy-node": (derived_key, ""),
        }

        async def hello(node_id: str, seed: bytes, key: str):
            return await _sync(
                tc,
                from_node=node_id,
                seed=seed,
                ip=GENUINE_IP,
                type_=NODE_HELLO,
                payload={"node_id": node_id, "url": "", "public_key": key},
            )

        # The shared-seed sibling is still a member, under our own key.
        assert (await hello("sibling", own_seed, own_key)).status == 200
        assert (
            await _sync(tc, from_node="sibling", seed=own_seed, ip=GENUINE_IP)
        ).status == 200

        # The foreign (TOFU) peer lost membership: re-add it to come back.
        resp = await hello("old-peer", foreign_seed, foreign_key)
        assert (resp.status, await resp.json()) == (403, {"error": "unapproved_node"})
        resp = await _sync(tc, from_node="old-peer", seed=foreign_seed, ip=GENUINE_IP)
        assert (resp.status, await resp.json()) == (403, {"error": "unapproved_node"})

        # A forger holding the legacy derived key gets nowhere either.
        resp = await hello("legacy-node", derived_seed, derived_key)
        assert (resp.status, await resp.json()) == (403, {"error": "unapproved_node"})
        resp = await _sync(
            tc, from_node="legacy-node", seed=derived_seed, ip=GENUINE_IP
        )
        assert (resp.status, await resp.json()) == (403, {"error": "unapproved_node"})

        # The admin view: the sibling shares our key; the others need re-adding.
        view = await app[gfs_cluster_key].admin_cluster()
        assert {
            n["node_id"]: n["key_source"] for n in view["nodes"] if not n["is_self"]
        } == {"sibling": "own", "old-peer": "none", "legacy-node": "none"}


@pytest.mark.security
async def test_a_foreign_peer_rejoins_once_an_admin_re_adds_it(client, outbound_hellos):
    """After the upgrade a distinct-key peer's row has no approved key (its
    legacy TOFU key is kept but untrusted); the operator re-adds it with its
    key, and from then on it syncs under that key."""
    seed, pub_hex = _keypair()
    repo = client._app[gfs_cluster_repo_key]
    await repo.insert_node(
        ClusterNode(
            node_id="old-peer",
            url="http://old-peer.test",
            public_key=pub_hex,
            status="online",
            last_seen="2026-01-01 00:00:00",
        )
    )
    resp = await _sync(client, from_node="old-peer", seed=seed, ip=GENUINE_IP)
    assert (resp.status, await resp.json()) == (403, {"error": "unapproved_node"})
    resp = await client.post(
        "/admin/api/cluster/peers",
        json={
            "node_id": "old-peer",
            "url": "http://old-peer.test",
            "public_key": pub_hex,
        },
    )
    assert resp.status == 201
    resp = await _sync(client, from_node="old-peer", seed=seed, ip=GENUINE_IP)
    assert resp.status == 200
    body = await (await client.get("/admin/api/cluster")).json()
    (row,) = [n for n in body["nodes"] if n["node_id"] == "old-peer"]
    assert (row["key_source"], row["public_key"]) == ("approved", pub_hex)


# ─── Recipient binding (signed ``to``) ────────────────────────────────


def _frame_to(seed: bytes, to: object, *, from_node: str = PEER) -> tuple[bytes, str]:
    return _sign_body(
        {
            "type": NODE_HEARTBEAT,
            "from": from_node,
            "to": to,
            "ts": int(time.time()),
            "nonce": secrets.token_urlsafe(16),
            "sig_suite": "ed25519",
            "payload": {},
        },
        seed,
    )


@pytest.mark.security
async def test_frame_for_another_node_is_refused_and_charged_to_the_address(
    client,
):
    """A frame signed for node X, replayed to us, is a 409 — charged to the
    replaying address, never to the peer it names."""
    seed = await _register_peer(client)
    canonical, sig = _frame_to(seed, "gfs-node-x")
    resp = await _post_raw(client, canonical, sig, ATTACKER_IP)
    assert (resp.status, await resp.json()) == (409, {"error": "wrong_recipient"})
    svc = client._app[gfs_cluster_key]
    assert PEER not in svc._sync_node_limiter
    for _ in range(CLUSTER_UNVERIFIED_RATE_LIMIT_PER_MIN - 1):
        canonical, sig = _frame_to(seed, "gfs-node-x")
        resp = await _post_raw(client, canonical, sig, ATTACKER_IP)
        assert resp.status == 409
    canonical, sig = _frame_to(seed, "gfs-node-x")
    assert (await _post_raw(client, canonical, sig, ATTACKER_IP)).status == 429


async def test_frame_addressed_to_us_is_accepted(client):
    seed = await _register_peer(client)
    canonical, sig = _frame_to(seed, "gfs-node-a")
    assert (await _post_raw(client, canonical, sig, GENUINE_IP)).status == 200


@pytest.mark.parametrize("bad", [7, None, ["gfs-node-a"], {"id": "gfs-node-a"}])
async def test_mistyped_recipient_is_malformed(client, bad):
    seed = await _register_peer(client)
    canonical, sig = _frame_to(seed, bad)
    resp = await _post_raw(client, canonical, sig, GENUINE_IP)
    assert (resp.status, await resp.json()) == (400, {"error": "invalid_message"})


async def test_frame_without_a_recipient_is_accepted_from_an_older_sender(client):
    """DEPRECATED behaviour, pinned on purpose: senders older than ``to``
    omit it, and a non-HELLO frame without it is still accepted (the
    compatibility tripwire, like a missing ``sig_suite``). The TODO at the
    recipient check in ``routes/cluster.py`` says when this flips to a 400
    ``missing_recipient`` — update this test then."""
    seed = await _register_peer(client)
    canonical, sig = _sign_body(
        {
            "type": NODE_HEARTBEAT,
            "from": PEER,
            "ts": int(time.time()),
            "nonce": secrets.token_urlsafe(16),
            "payload": {},
        },
        seed,
    )
    assert (await _post_raw(client, canonical, sig, GENUINE_IP)).status == 200


# ─── Peer URL hygiene (admin add-peer, HELLO, /cluster/health) ────────

_UNSAFE_PEER_URLS = [
    "http://ex\nample.com",
    "http://h:80\r\nX-Inj: 1",
    "http://h/\x00",
    "http://a b.com",
    "http://h:99999",
    "http://‮evil.com",
    "http://q.test/‮evil\r\nX: 1",
]


@pytest.mark.security
@pytest.mark.parametrize("url", _UNSAFE_PEER_URLS)
async def test_admin_add_peer_refuses_an_unsafe_url(client, url):
    resp = await client.post(
        "/admin/api/cluster/peers",
        json={"node_id": "c", "url": url, "public_key": _VALID_KEY},
    )
    assert (resp.status, await resp.json()) == (422, {"error": "invalid_url"})


@pytest.mark.security
@pytest.mark.parametrize("url", _UNSAFE_PEER_URLS)
async def test_an_unsafe_hello_url_is_never_stored_or_echoed(client, url):
    """A shared-seed sibling's first HELLO carries its URL; an unsafe one is
    dropped, so the public ``/cluster/health`` never echoes it."""
    own_seed = client._app[gfs_cluster_key]._signing_key
    own_key = client._app[gfs_cluster_key].own_public_key_hex
    resp = await _sync(
        client,
        from_node="sibling",
        seed=own_seed,
        ip=GENUINE_IP,
        type_=NODE_HELLO,
        payload={"node_id": "sibling", "url": url, "public_key": own_key},
    )
    assert resp.status == 200
    health = await (await client.get("/cluster/health")).text()
    peers = {p["node_id"]: p for p in json.loads(health)["peers"]}
    assert "url" not in peers["sibling"]
    for needle in ("X-Inj", "evil", "\\u202e", "\\r", "\\n", "\\u0000"):
        assert needle not in health


@pytest.mark.security
async def test_cluster_health_never_lists_peer_urls(client):
    """The public health page lists who is in the cluster, not where: with
    ``advertise_url`` a peer row holds an internal node address + port, so
    publishing it would expose the cluster's internal topology. URLs are
    admin-only (``GET /admin/api/cluster``)."""
    own_seed = client._app[gfs_cluster_key]._signing_key
    own_key = client._app[gfs_cluster_key].own_public_key_hex
    await _register_peer(client)
    resp = await _sync(
        client,
        from_node="sibling",
        seed=own_seed,
        ip=GENUINE_IP,
        type_=NODE_HELLO,
        payload={
            "node_id": "sibling",
            "url": "http://10.0.0.5:28467",
            "public_key": own_key,
        },
    )
    assert resp.status == 200
    health = await (await client.get("/cluster/health")).text()
    peers = json.loads(health)["peers"]
    assert {p["node_id"] for p in peers} == {PEER, "sibling"}
    for peer in peers:
        assert set(peer) == {"node_id", "status", "last_seen"}
    for needle in ("10.0.0.5", "28467", f"{PEER}.test"):
        assert needle not in health
    admin = await (await client.get("/admin/api/cluster")).json()
    urls = {n["node_id"]: n["url"] for n in admin["nodes"]}
    assert urls["sibling"] == "http://10.0.0.5:28467"


@pytest.mark.security
async def test_own_key_hello_moves_a_sibling_url_a_foreign_key_cannot(client):
    """A sibling redeployed on a new port re-HELLOs under our shared seed and
    its row follows; a HELLO for the same node id under any other key is
    refused and the URL stays put."""
    svc = client._app[gfs_cluster_key]
    own_seed = svc._signing_key
    own_key = svc.own_public_key_hex
    for url in ("http://10.0.0.5:1111", "http://10.0.0.5:2222"):
        resp = await _sync(
            client,
            from_node="sibling",
            seed=own_seed,
            ip=GENUINE_IP,
            type_=NODE_HELLO,
            payload={"node_id": "sibling", "url": url, "public_key": own_key},
        )
        assert resp.status == 200
    repo = client._app[gfs_cluster_repo_key]
    (row,) = [n for n in await repo.list_nodes() if n.node_id == "sibling"]
    assert row.url == "http://10.0.0.5:2222"
    seed, pub_hex = _keypair()
    resp = await _sync(
        client,
        from_node="sibling",
        seed=seed,
        ip=ATTACKER_IP,
        type_=NODE_HELLO,
        payload={
            "node_id": "sibling",
            "url": "http://evil.test",
            "public_key": pub_hex,
        },
    )
    assert resp.status == 403
    (row,) = [n for n in await repo.list_nodes() if n.node_id == "sibling"]
    assert row.url == "http://10.0.0.5:2222"


@pytest.mark.security
@pytest.mark.parametrize(
    "url",
    ["http://169.254.169.254", "http://[fe80::1]:8000", "http://[fd00:ec2::254]"],
)
async def test_admin_add_peer_refuses_a_link_local_or_metadata_url(client, url):
    resp = await client.post(
        "/admin/api/cluster/peers",
        json={"node_id": "c", "url": url, "public_key": _VALID_KEY},
    )
    assert (resp.status, await resp.json()) == (422, {"error": "invalid_url"})


# ─── Failed-verify budget per (node, address) ─────────────────────────


async def _shed(client, ip: str) -> None:
    """Spend *ip*'s unverified budget with junk (XFF-spoofable under the
    default ``trusted_proxies``)."""
    for _ in range(CLUSTER_UNVERIFIED_RATE_LIMIT_PER_MIN):
        await client.post(
            "/cluster/sync", data=b"junk", headers={"X-Forwarded-For": ip}
        )


@pytest.mark.security
async def test_forgeries_from_other_hosts_cannot_lock_a_member_out(client):
    """The review's probe: 30 junk frames spoofing the member's address (it
    is shed), then 30 forged frames naming the member from another shed
    host — the member's genuine frame from its own address still gets 200.
    The failed-verify budget is per (node, address), so forgeries from
    elsewhere cannot spend the one the member's address uses."""
    seed = await _register_peer(client)
    await _shed(client, GENUINE_IP)
    await _shed(client, ATTACKER_IP)
    for _ in range(CLUSTER_FAILED_VERIFY_RATE_LIMIT_PER_MIN):
        resp = await _sync(client, from_node=PEER, seed=None, ip=ATTACKER_IP)
        assert resp.status == 429
    resp = await _sync(client, from_node=PEER, seed=seed, ip=GENUINE_IP)
    assert resp.status == 200


@pytest.mark.security
async def test_failed_verifies_per_node_have_a_global_ceiling(
    client, clock, verify_calls, monkeypatch
):
    """Forgeries spread over many shed addresses are bounded per node too,
    so verify CPU stays bounded whatever the address count."""
    monkeypatch.setattr(
        client._app[gfs_cluster_key]._sync_failed_verify_node_limiter, "_limit", 40
    )
    await _register_peer(client)
    ips = [f"10.9.0.{i}" for i in range(1, 4)]
    for ip in ips:
        await _shed(client, ip)
    verify_calls.clear()
    for ip in ips:
        for _ in range(CLUSTER_FAILED_VERIFY_RATE_LIMIT_PER_MIN):
            await _sync(client, from_node=PEER, seed=None, ip=ip)
    assert len(verify_calls) == 40
    clock.now += 61
    await _sync(client, from_node=PEER, seed=None, ip=ips[0])
    assert len(verify_calls) == 41


@pytest.mark.security
async def test_every_rejection_from_a_shed_address_is_429(client):
    """A failed verify from a shed address is answered like every other
    rejection from it — 429, not 401."""
    await _register_peer(client)
    await _shed(client, ATTACKER_IP)
    resp = await _sync(client, from_node=PEER, seed=None, ip=ATTACKER_IP)
    assert (resp.status, await resp.json()) == (429, {"error": "rate_limited"})
    assert resp.headers["Retry-After"] == "60"


# ─── Replay-cache room and roster size ────────────────────────────────


@pytest.mark.security
async def test_a_node_over_its_replay_share_is_refused_503(client, monkeypatch):
    """A frame the replay cache has no room for is refused — never let in
    by evicting a live digest — and another node is unaffected."""
    seed = await _register_peer(client)
    other = await _register_peer(client, "gfs-node-c")
    monkeypatch.setattr(client._app[gfs_cluster_key]._seen_frames, "_per_node_cap", 2)
    for _ in range(2):
        resp = await _sync(client, from_node=PEER, seed=seed, ip=GENUINE_IP)
        assert resp.status == 200
    resp = await _sync(client, from_node=PEER, seed=seed, ip=GENUINE_IP)
    assert (resp.status, await resp.json()) == (503, {"error": "replay_cache_full"})
    assert resp.headers["Retry-After"] == "60"
    resp = await _sync(client, from_node="gfs-node-c", seed=other, ip=GENUINE_IP)
    assert resp.status == 200


@pytest.mark.security
async def test_own_key_hellos_cannot_grow_the_roster_without_bound(client):
    """A seed holder's first HELLO creates a row only while fewer than
    ``CLUSTER_MAX_NODES`` peers are on the roster."""
    svc = client._app[gfs_cluster_key]
    own_seed, own_key = svc._signing_key, svc.own_public_key_hex
    repo = client._app[gfs_cluster_repo_key]
    for i in range(CLUSTER_MAX_NODES):
        await repo.insert_node(
            ClusterNode(node_id=f"sib-{i}", url="", public_key=own_key)
        )

    async def hello(node_id: str):
        return await _sync(
            client,
            from_node=node_id,
            seed=own_seed,
            ip=GENUINE_IP,
            type_=NODE_HELLO,
            payload={"node_id": node_id, "url": "", "public_key": own_key},
        )

    resp = await hello("sib-new")
    assert (resp.status, await resp.json()) == (403, {"error": "cluster_full"})
    assert "sib-new" not in {n.node_id for n in await repo.list_nodes()}
    # A sibling already on the roster still gets through.
    assert (await hello("sib-0")).status == 200
    # And once one is removed, a new one can join.
    await repo.remove_node("sib-1")
    assert (await hello("sib-new")).status == 200


# ─── Stale shared-seed siblings (Nomad allocation churn) ─────────────────

_LONG_AGO = "2026-01-01 00:00:00"


async def _own_key_hello(client, node_id: str):
    svc = client._app[gfs_cluster_key]
    return await _sync(
        client,
        from_node=node_id,
        seed=svc._signing_key,
        ip=GENUINE_IP,
        type_=NODE_HELLO,
        payload={"node_id": node_id, "url": "", "public_key": svc.own_public_key_hex},
    )


async def test_stale_siblings_are_collected_so_a_new_one_can_join(client, monkeypatch):
    """Every Nomad allocation is a new node id under the shared seed, and
    rows were never cleaned up: after ``CLUSTER_MAX_NODES`` allocations the
    next sibling got ``cluster_full`` for good. The heartbeat loop now drops
    shared-seed rows unseen for :data:`CLUSTER_STALE_SIBLING_S`."""
    svc = client._app[gfs_cluster_key]
    monkeypatch.setattr(ClusterService, "_ping_peer", _never_reachable)
    repo = client._app[gfs_cluster_repo_key]
    for i in range(CLUSTER_MAX_NODES):
        await repo.insert_node(
            ClusterNode(
                node_id=f"alloc-{i}",
                url=f"http://alloc-{i}.test",
                public_key=svc.own_public_key_hex,
                status="offline",
                last_seen=_LONG_AGO,
            )
        )
    resp = await _own_key_hello(client, "alloc-new")
    assert (resp.status, await resp.json()) == (403, {"error": "cluster_full"})
    await svc._heartbeat_tick()
    assert await repo.list_nodes() == []
    assert (await _own_key_hello(client, "alloc-new")).status == 200


async def _never_reachable(self, peer_url: str) -> bool:
    return False


async def test_collection_keeps_approved_active_and_unapproved_rows(
    client, monkeypatch
):
    """Only a shared-seed sibling's row is collected, and only once stale:
    an operator-approved row never is (however long it was offline), nor an
    active sibling, nor a row that is no member — the admin view shows it
    so the operator can re-add a distinct-key peer."""
    svc = client._app[gfs_cluster_key]
    monkeypatch.setattr(ClusterService, "_ping_peer", _never_reachable)
    repo = client._app[gfs_cluster_repo_key]
    seed, key = _keypair()
    await repo.approve_node("approved", "http://approved.test", key)
    await repo.touch_node("approved", status="offline", last_seen=_LONG_AGO)
    await repo.insert_node(
        ClusterNode(
            node_id="active-sibling",
            url="http://active.test",
            public_key=svc.own_public_key_hex,
            status="online",
            last_seen=_now_iso(),
        )
    )
    await repo.insert_node(
        ClusterNode(
            node_id="stale-sibling",
            url="http://stale.test",
            public_key=svc.own_public_key_hex,
            status="offline",
            last_seen=_LONG_AGO,
        )
    )
    await repo.insert_node(
        ClusterNode(
            node_id="old-peer",
            url="http://old-peer.test",
            public_key=key,
            status="offline",
            last_seen=_LONG_AGO,
        )
    )
    await svc._heartbeat_tick()
    assert {n.node_id for n in await repo.list_nodes()} == {
        "approved",
        "active-sibling",
        "old-peer",
    }


@pytest.mark.security
async def test_only_shared_seed_rows_count_towards_cluster_full(client, tmp_dir):
    """Rows an operator approved, and rows an old-version node wrote by
    trust-on-first-use, do not fill the own-key HELLO cap."""
    repo = client._app[gfs_cluster_repo_key]
    for i in range(CLUSTER_MAX_NODES):
        _, key = _keypair()
        await repo.approve_node(f"approved-{i}", f"http://a{i}.test", key)
        await repo.insert_node(
            ClusterNode(node_id=f"tofu-{i}", url=f"http://t{i}.test", public_key=key)
        )
    assert (await _own_key_hello(client, "sibling")).status == 200


# ─── Admin approval race ──────────────────────────────────────────────────


@pytest.mark.security
async def test_a_losing_concurrent_approval_moves_nothing_and_is_a_conflict(
    client, monkeypatch
):
    """Two admins approve the same node id with different keys and URLs at
    once: both pass the "already approved?" read before either writes. The
    first write wins whole — key and URL — and the second gets 409
    ``key_mismatch`` instead of a 201 for an approval that did not happen."""
    repo = client._app[gfs_cluster_repo_key]
    _, winner_key = _keypair()
    _, loser_key = _keypair()
    real_approve = SqliteClusterRepo.approve_node

    async def racing_approve(self, node_id, url, approved_key):
        # The other admin's approval commits between our read and write.
        await real_approve(self, node_id, "http://winner.test", winner_key)
        await real_approve(self, node_id, url, approved_key)

    monkeypatch.setattr(SqliteClusterRepo, "approve_node", racing_approve)
    resp = await client.post(
        "/admin/api/cluster/peers",
        json={"node_id": "node-c", "url": "http://loser.test", "public_key": loser_key},
    )
    assert (resp.status, await resp.json()) == (409, {"error": "key_mismatch"})
    (row,) = await repo.list_nodes()
    assert (row.approved_key, row.approved_url, row.url) == (
        winner_key,
        "http://winner.test",
        "http://winner.test",
    )
