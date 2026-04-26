"""Tests for ``/cluster/signaling-session*`` (spec §24.10.7).

End-to-end through ``aiohttp.test_utils.TestClient`` so the full
signature-verify + service path runs. Pairs the SH provider's
``POST /cluster/signaling-session`` (pick a node, increment count) with
``POST /cluster/signaling-session/release`` (decrement on
``SPACE_SYNC_DIRECT_READY``/``DIRECT_FAILED``).
"""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.crypto import (
    b64url_encode,
    generate_identity_keypair,
    sign_ed25519,
)
from socialhome.global_server.app_keys import (
    gfs_cluster_key,
    gfs_cluster_repo_key,
    gfs_fed_repo_key,
)
from socialhome.global_server.cluster import MAX_SIGNALING_SESSIONS
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance, ClusterNode
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
async def signed_caller(tmp_dir):
    """Spin up a GFS app + register a paired client instance.

    Yields ``(client, sign)`` where ``sign(body)`` returns ``body`` with
    a valid signature attached, ready to POST.
    """
    app = create_gfs_app(_config(tmp_dir))
    async with TestClient(TestServer(app)) as tc:
        kp = generate_identity_keypair()
        fed_repo = app[gfs_fed_repo_key]
        await fed_repo.upsert_instance(
            ClientInstance(
                instance_id="caller.home",
                display_name="Caller",
                public_key=kp.public_key.hex(),
                inbox_url="http://caller.home/wh",
                status="active",
                auto_accept=True,
            )
        )
        # Self row so the URL→node_id reverse-lookup resolves.
        cluster_repo = app[gfs_cluster_repo_key]
        await cluster_repo.upsert_node(
            ClusterNode(
                node_id="gfs-node-a",
                url="http://gfs.test",
                status="online",
            )
        )

        def sign(body: dict) -> dict:
            canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
            sig = b64url_encode(sign_ed25519(kp.private_key, canonical))
            return {**body, "signature": sig}

        tc._app = app
        yield tc, sign


# ─── Begin endpoint ──────────────────────────────────────────────────


async def test_signaling_session_returns_url_and_increments(signed_caller):
    tc, sign = signed_caller
    body = sign({"from_instance": "caller.home", "sync_id": "s-1"})
    resp = await tc.post("/cluster/signaling-session", json=body)
    assert resp.status == 200, await resp.text()
    payload = await resp.json()
    assert payload["session_id"] == "s-1"
    assert payload["signaling_node"] == "http://gfs.test"
    cluster = tc._app[gfs_cluster_key]
    assert cluster._active_sync_count["gfs-node-a"] == 1


async def test_signaling_session_picks_least_loaded_peer(signed_caller):
    tc, sign = signed_caller
    cluster_repo = tc._app[gfs_cluster_repo_key]
    cluster = tc._app[gfs_cluster_key]
    # Add an idle peer; load self up so the peer wins.
    await cluster_repo.upsert_node(
        ClusterNode(node_id="gfs-node-b", url="http://b.gfs.test", status="online"),
    )
    cluster._active_sync_count["gfs-node-a"] = 50

    body = sign({"from_instance": "caller.home", "sync_id": "s-2"})
    resp = await tc.post("/cluster/signaling-session", json=body)
    payload = await resp.json()
    assert payload["signaling_node"] == "http://b.gfs.test"
    assert cluster._active_sync_count["gfs-node-b"] == 1
    assert cluster._active_sync_count["gfs-node-a"] == 50  # untouched


async def test_signaling_session_returns_503_when_all_at_cap(signed_caller):
    tc, sign = signed_caller
    cluster_repo = tc._app[gfs_cluster_repo_key]
    cluster = tc._app[gfs_cluster_key]
    await cluster_repo.upsert_node(
        ClusterNode(node_id="gfs-node-b", url="http://b.gfs.test", status="online"),
    )
    cluster._active_sync_count["gfs-node-a"] = MAX_SIGNALING_SESSIONS
    cluster._active_sync_count["gfs-node-b"] = MAX_SIGNALING_SESSIONS

    body = sign({"from_instance": "caller.home", "sync_id": "s-3"})
    resp = await tc.post("/cluster/signaling-session", json=body)
    assert resp.status == 503
    payload = await resp.json()
    assert payload == {"reason": "node_capacity"}


async def test_signaling_session_unknown_caller_is_403(signed_caller):
    tc, _sign = signed_caller
    body = {
        "from_instance": "ghost.home",
        "sync_id": "s-x",
        "signature": "AA",
    }
    resp = await tc.post("/cluster/signaling-session", json=body)
    assert resp.status == 403


async def test_signaling_session_invalid_signature_is_401(signed_caller):
    tc, _sign = signed_caller
    body = {
        "from_instance": "caller.home",
        "sync_id": "s-bad",
        "signature": b64url_encode(b"\x00" * 64),
    }
    resp = await tc.post("/cluster/signaling-session", json=body)
    assert resp.status == 401


async def test_signaling_session_missing_sync_id_is_400(signed_caller):
    tc, sign = signed_caller
    body = sign({"from_instance": "caller.home"})
    resp = await tc.post("/cluster/signaling-session", json=body)
    assert resp.status == 400


async def test_signaling_session_single_node_returns_null(tmp_dir):
    """``cluster_enabled=false`` → caller-omits-field semantics."""
    app = create_gfs_app(_config(tmp_dir, cluster=False))
    async with TestClient(TestServer(app)) as tc:
        kp = generate_identity_keypair()
        await app[gfs_fed_repo_key].upsert_instance(
            ClientInstance(
                instance_id="caller.home",
                display_name="Caller",
                public_key=kp.public_key.hex(),
                inbox_url="http://caller.home/wh",
                status="active",
                auto_accept=True,
            )
        )
        body = {"from_instance": "caller.home", "sync_id": "s-1"}
        canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        body["signature"] = b64url_encode(
            sign_ed25519(kp.private_key, canonical),
        )
        resp = await tc.post("/cluster/signaling-session", json=body)
        assert resp.status == 200
        payload = await resp.json()
        assert payload["signaling_node"] is None


# ─── Release endpoint ────────────────────────────────────────────────


async def test_release_decrements_count(signed_caller):
    tc, sign = signed_caller
    cluster = tc._app[gfs_cluster_key]
    # Begin → count=1
    await tc.post(
        "/cluster/signaling-session",
        json=sign({"from_instance": "caller.home", "sync_id": "s-rel"}),
    )
    assert cluster._active_sync_count["gfs-node-a"] == 1
    # Release → count=0
    resp = await tc.post(
        "/cluster/signaling-session/release",
        json=sign(
            {
                "from_instance": "caller.home",
                "sync_id": "s-rel",
                "signaling_node": "http://gfs.test",
            },
        ),
    )
    assert resp.status == 200
    assert cluster._active_sync_count["gfs-node-a"] == 0


async def test_release_is_idempotent(signed_caller):
    tc, sign = signed_caller
    cluster = tc._app[gfs_cluster_key]
    # Two releases without a begin → floors at 0.
    for _ in range(2):
        resp = await tc.post(
            "/cluster/signaling-session/release",
            json=sign(
                {
                    "from_instance": "caller.home",
                    "sync_id": "s-dup",
                    "signaling_node": "http://gfs.test",
                },
            ),
        )
        assert resp.status == 200
    assert cluster._active_sync_count.get("gfs-node-a", 0) == 0


async def test_release_unknown_signaling_node_is_silent(signed_caller):
    """Stale URL (peer removed mid-flight) → 200, no-op, no error.

    Keeps the SH provider's release path simple.
    """
    tc, sign = signed_caller
    resp = await tc.post(
        "/cluster/signaling-session/release",
        json=sign(
            {
                "from_instance": "caller.home",
                "sync_id": "s-stale",
                "signaling_node": "http://nowhere.test",
            },
        ),
    )
    assert resp.status == 200


async def test_release_missing_signaling_node_is_400(signed_caller):
    tc, sign = signed_caller
    body = sign({"from_instance": "caller.home", "sync_id": "s-x"})
    resp = await tc.post("/cluster/signaling-session/release", json=body)
    assert resp.status == 400
