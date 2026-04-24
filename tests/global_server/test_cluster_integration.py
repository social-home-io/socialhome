"""Integration tests for the GFS cluster mode (spec §24.10).

Exercises the NODE_* dispatch, ban-wins LWW, /cluster/health, and the
admin /admin/api/cluster endpoints. Uses an in-process aiohttp
:class:`TestClient` so the full HTTP signature + verification path runs.
"""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.crypto import b64url_encode, sign_ed25519
from socialhome.global_server.admin import hash_password
from socialhome.global_server.app_keys import (
    gfs_admin_repo_key,
    gfs_cluster_key,
    gfs_cluster_repo_key,
    gfs_fed_repo_key,
)
from socialhome.global_server.cluster import (
    NODE_HEARTBEAT,
    NODE_HELLO,
)
from socialhome.global_server.config import GfsConfig
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


@pytest.fixture
async def client(tmp_dir):
    app = create_gfs_app(_config(tmp_dir))
    async with TestClient(TestServer(app)) as tc:
        # Seed an admin password so the admin routes accept our cookie.
        await app[gfs_admin_repo_key].set_config(
            "admin_password_hash",
            hash_password("admin-pw"),
        )
        await tc.post("/admin/login", json={"password": "admin-pw"})
        tc._app = app
        yield tc


def _post_node_payload(
    type_: str, payload: dict, *, from_node: str, signing_key: bytes
):
    body = {
        "type": type_,
        "from": from_node,
        "ts": 1700000000,
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


async def test_node_hello_registers_peer(client):
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization

    priv = ed25519.Ed25519PrivateKey.generate()
    seed = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_hex = (
        priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
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


async def test_cluster_sync_unknown_node_is_403(client):
    """An unregistered peer can only send NODE_HELLO (TOFU); anything
    else gets 403 without signature verification.
    """
    canonical = json.dumps(
        {"type": NODE_HEARTBEAT, "from": "ghost", "ts": 1700000000, "payload": {}},
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


async def test_cluster_sync_rate_limits_per_node(client):
    """The same peer hitting /cluster/sync > 60x/min → 429."""
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization

    priv = ed25519.Ed25519PrivateKey.generate()
    seed = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_hex = (
        priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    # Register the peer first via NODE_HELLO.
    canonical, sig = _post_node_payload(
        NODE_HELLO,
        {"node_id": "flood", "url": "http://flood", "public_key": pub_hex},
        from_node="flood",
        signing_key=seed,
    )
    await client.post(
        "/cluster/sync",
        data=canonical,
        headers={
            "Content-Type": "application/json",
            "X-Node-Signature": sig,
            "X-Node-Id": "flood",
        },
    )
    # 59 further NODE_HEARTBEATs — NODE_HELLO was #1, so we land on
    # 60 (the cap).
    for _ in range(59):
        canonical, sig = _post_node_payload(
            NODE_HEARTBEAT,
            {},
            from_node="flood",
            signing_key=seed,
        )
        resp = await client.post(
            "/cluster/sync",
            data=canonical,
            headers={
                "Content-Type": "application/json",
                "X-Node-Signature": sig,
                "X-Node-Id": "flood",
            },
        )
        assert resp.status == 200
    # 61st request crosses the cap → 429.
    canonical, sig = _post_node_payload(
        NODE_HEARTBEAT,
        {},
        from_node="flood",
        signing_key=seed,
    )
    resp = await client.post(
        "/cluster/sync",
        data=canonical,
        headers={
            "Content-Type": "application/json",
            "X-Node-Signature": sig,
            "X-Node-Id": "flood",
        },
    )
    assert resp.status == 429
