"""Integration tests for the GFS cluster mode (spec §24.10).

Exercises the NODE_* dispatch, ban-wins LWW, /cluster/health, and the
admin /admin/api/cluster endpoints. Uses an in-process aiohttp
:class:`TestClient` so the full HTTP signature + verification path runs.
"""

from __future__ import annotations

import json
import secrets
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.crypto import b64url_encode, ed25519_public_key, sign_ed25519
from socialhome.global_server.admin import hash_password
from socialhome.global_server.app_keys import (
    gfs_admin_repo_key,
    gfs_cluster_key,
    gfs_cluster_repo_key,
    gfs_fed_repo_key,
)
from socialhome.global_server.cluster import (
    CLUSTER_RATE_LIMIT_PER_MIN,
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
from socialhome.global_server.server import create_gfs_app


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
    seed, pub_hex = _keypair()
    canonical, sig = _post_node_payload(
        NODE_HELLO,
        {"node_id": "gfs-node-b", "url": "http://b.test", "public_key": pub_hex},
        from_node="gfs-node-b",
        signing_key=seed,
    )
    resp = await client.post(
        "/cluster/sync",
        data=canonical,
        headers={
            "Content-Type": "application/json",
            "X-Node-Signature": sig,
            "X-Node-Id": "gfs-node-b",
        },
    )
    assert resp.status == 200
    # Peer is now in cluster_nodes.
    cluster_repo = client._app[gfs_cluster_repo_key]
    nodes = await cluster_repo.list_nodes()
    assert any(n.node_id == "gfs-node-b" for n in nodes)
    # First contact → we HELLO back (stopped at the boundary, never sent).
    assert outbound_hellos == [("http://b.test", NODE_HELLO)]


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


async def test_admin_cluster_add_and_remove_peer(client):
    from urllib.parse import quote

    resp = await client.post(
        "/admin/api/cluster/peers",
        json={"url": "http://peer-c.test"},
    )
    assert resp.status == 201
    body = await resp.json()
    # Peer is now in cluster_nodes.
    cluster_repo = client._app[gfs_cluster_repo_key]
    nodes = await cluster_repo.list_nodes()
    assert any(n.url == "http://peer-c.test" for n in nodes)
    # Delete — the URL-shaped node_id must be percent-encoded in the path.
    resp = await client.delete(
        f"/admin/api/cluster/peers/{quote(body['node_id'], safe='')}",
    )
    assert resp.status == 200
    nodes_after = await cluster_repo.list_nodes()
    assert not any(n.node_id == body["node_id"] for n in nodes_after)


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
async def test_tofu_hellos_spend_the_address_budget_not_a_node_budget(
    client, outbound_hellos
):
    """A first-contact HELLO is self-signed under the key it carries, so it
    proves nothing about who sent it — it is charged to the source address.
    A HELLO from a known peer under its pinned key is that peer's traffic."""
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
        assert resp.status == 200
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
    # A known peer re-announcing under its pinned key is that peer's own
    # (verified) traffic — charged to its node budget, not to an address.
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
