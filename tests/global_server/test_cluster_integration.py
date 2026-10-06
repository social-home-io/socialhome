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
    CLUSTER_RATE_LIMIT_PER_MIN,
    CLUSTER_REPLAY_MAX_ENTRIES,
    CLUSTER_REPLAY_SIZED_NODES,
    CLUSTER_REPLAY_TTL_S,
    CLUSTER_TS_SKEW_S,
    CLUSTER_UNVERIFIED_RATE_LIMIT_PER_MIN,
    NODE_HEARTBEAT,
    NODE_HELLO,
    ClusterService,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClusterNode
from socialhome.global_server.public import RATE_LIMIT_MAX_TRACKED_IPS
from socialhome.global_server.repositories import SqliteClusterRepo
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

    async def _fake_post(self, peer_url, msg_type, payload, *, session=None):
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
    assert row.public_key == pinned_hex
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
            "inbox_url": "http://peer/wh",
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
            "inbox_url": "http://o/wh",
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
    assert row.public_key == pinned
    resp = await client.post(
        "/admin/api/cluster/peers",
        json={"node_id": PEER, "url": "http://b2.test", "public_key": pinned},
    )
    assert resp.status == 201
    (row,) = await client._app[gfs_cluster_repo_key].list_nodes()
    assert (row.url, row.public_key, row.status) == ("http://b2.test", pinned, "online")


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
    await client._app[gfs_cluster_repo_key].upsert_node(
        ClusterNode(
            node_id=node_id,
            url=f"http://{node_id}.test",
            public_key=pub_hex,
            status="online",
        )
    )
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


@pytest.mark.security
async def test_unverified_flood_hits_the_per_address_bound(client, clock):
    """Failed requests are shed per source address BEFORE any parse, DB read
    or signature verification, so a forged flood can't burn verify CPU."""
    await _register_peer(client)
    for _ in range(CLUSTER_UNVERIFIED_RATE_LIMIT_PER_MIN):
        resp = await _sync(client, from_node=PEER, seed=None, ip=ATTACKER_IP)
        assert resp.status == 401
    for bad in (b"not json", b"[]", b'{"type": "x"}'):
        resp = await client.post(
            "/cluster/sync", data=bad, headers={"X-Forwarded-For": ATTACKER_IP}
        )
        assert resp.status == 429
    resp = await _sync(client, from_node="ghost", seed=None, ip=ATTACKER_IP)
    assert resp.status == 429
    # Another address still gets a real answer.
    resp = await _sync(client, from_node="ghost", seed=None, ip=GENUINE_IP)
    assert resp.status == 403
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
    expiring but while still fresh) and hold every frame the verified
    budget can admit for the sized roster within its TTL (else eviction
    reopens the window)."""
    assert CLUSTER_REPLAY_TTL_S >= 2 * CLUSTER_TS_SKEW_S
    assert CLUSTER_REPLAY_MAX_ENTRIES == (
        CLUSTER_RATE_LIMIT_PER_MIN
        * int(CLUSTER_REPLAY_TTL_S // 60)
        * CLUSTER_REPLAY_SIZED_NODES
    )
    svc = ClusterService(SqliteClusterRepo(gfs_db))
    assert svc._seen_frames._cap == CLUSTER_REPLAY_MAX_ENTRIES
    assert svc._seen_frames._ttl == CLUSTER_REPLAY_TTL_S


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
async def test_upgrade_clears_every_pin_and_only_shared_seed_siblings_rejoin(
    tmp_dir, outbound_hellos
):
    """Pins from before operator approval may be TOFU intruders or legacy
    DERIVED keys anyone can compute. The upgrade clears them all: a
    shared-seed sibling re-pins on its next HELLO, while a distinct-key peer
    — and a forger holding a derived key — must be re-added by an admin."""
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
        assert {n.node_id: n.public_key for n in await repo.list_nodes()} == {
            "sibling": "",
            "old-peer": "",
            "legacy-node": "",
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

        # The shared-seed sibling re-pins under our own key.
        assert (await hello("sibling", own_seed, own_key)).status == 200
        pins = {n.node_id: n.public_key for n in await repo.list_nodes()}
        assert pins["sibling"] == own_key
        assert (
            await _sync(tc, from_node="sibling", seed=own_seed, ip=GENUINE_IP)
        ).status == 200

        # The foreign (TOFU) peer lost membership: re-add it to come back.
        resp = await hello("old-peer", foreign_seed, foreign_key)
        assert (resp.status, await resp.json()) == (403, {"error": "unapproved_node"})
        resp = await _sync(tc, from_node="old-peer", seed=foreign_seed, ip=GENUINE_IP)
        assert (resp.status, await resp.json()) == (401, {"error": "invalid_signature"})

        # A forger holding the legacy derived key gets nowhere either.
        resp = await hello("legacy-node", derived_seed, derived_key)
        assert (resp.status, await resp.json()) == (403, {"error": "unapproved_node"})
        resp = await _sync(
            tc, from_node="legacy-node", seed=derived_seed, ip=GENUINE_IP
        )
        assert (resp.status, await resp.json()) == (401, {"error": "invalid_signature"})
        pins = {n.node_id: n.public_key for n in await repo.list_nodes()}
        assert pins["old-peer"] == pins["legacy-node"] == ""


@pytest.mark.security
async def test_a_foreign_peer_rejoins_once_an_admin_re_adds_it(client, outbound_hellos):
    """After the upgrade a distinct-key peer's row has no pin; the operator
    re-adds it with its key, and from then on it syncs under that pin."""
    seed, pub_hex = _keypair()
    repo = client._app[gfs_cluster_repo_key]
    await repo.upsert_node(
        ClusterNode(
            node_id="old-peer",
            url="http://old-peer.test",
            public_key="",
            status="online",
            last_seen="2026-01-01 00:00:00",
        )
    )
    resp = await _sync(client, from_node="old-peer", seed=seed, ip=GENUINE_IP)
    assert resp.status == 401
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
    assert row["key_source"] == "pinned"
