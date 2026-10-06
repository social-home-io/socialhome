"""§27.9 release blocker: only operator-approved nodes join a GFS cluster.

``POST /cluster/sync`` used to trust a first-contact ``NODE_HELLO`` under
whatever key it carried (TOFU): anyone who could reach the endpoint joined
the cluster and could push client, space, report and policy state into
it. A captured frame also stayed valid forever — ``ts`` was never checked
and nothing remembered an accepted frame.

The rule these tests pin, end to end over a real GFS server with real
``ClusterService`` senders:

* a node is a member if and only if its frames verify under a key the
  receiving GFS already holds — its own identity key (the shared seed the
  operator gave the node) or a key an operator pinned through
  ``POST /admin/api/cluster/peers``;
* an unknown HELLO is refused and writes nothing;
* a pinned key is never overwritten in-band;
* a replayed or stale frame is refused;
* a peer that was already syncing before the upgrade keeps syncing.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp import ClientSession, CookieJar
from aiohttp.test_utils import TestServer

from socialhome.crypto import b64url_encode, ed25519_public_key, sign_ed25519
from socialhome.global_server.admin import hash_password
from socialhome.global_server.app_keys import (
    gfs_admin_repo_key,
    gfs_cluster_key,
    gfs_cluster_repo_key,
)
from socialhome.global_server.cluster import (
    CLUSTER_TS_SKEW_S,
    NODE_HEARTBEAT,
    NODE_HELLO,
    ClusterService,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClusterNode
from socialhome.global_server.server import create_gfs_app

pytestmark = pytest.mark.security

#: A sender's advertised URL nobody listens on: the receiver's reply HELLO
#: fails fast instead of leaving the process.
_UNREACHABLE = "http://127.0.0.1:1"


class _Roster:
    """In-memory roster for a sender-side ``ClusterService``."""

    def __init__(self) -> None:
        self.rows: dict[str, ClusterNode] = {}

    async def upsert_node(self, node: ClusterNode) -> None:
        self.rows[node.node_id] = node

    async def list_nodes(self) -> list[ClusterNode]:
        return list(self.rows.values())

    async def remove_node(self, node_id: str) -> None:
        self.rows.pop(node_id, None)

    async def update_active_sync_sessions(self, node_id: str, count: int) -> None:
        return None


def _sender(node_id: str, seed: bytes) -> ClusterService:
    """A real sender node: signs frames exactly as production does."""
    return ClusterService(
        _Roster(),
        node_id=node_id,
        self_url=_UNREACHABLE,
        signing_key=seed,
        own_public_key_hex=ed25519_public_key(seed).hex(),
        enabled=True,
    )


def _frame(
    seed: bytes,
    *,
    type_: str,
    from_node: str,
    payload: dict,
    ts: int | None = None,
    current_shape: bool = True,
) -> tuple[bytes, str]:
    body: dict = {
        "type": type_,
        "from": from_node,
        "ts": int(time.time()) if ts is None else ts,
        "payload": payload,
    }
    if current_shape:
        body["nonce"] = b64url_encode(secrets.token_bytes(16))
        body["sig_suite"] = "ed25519"
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return raw, b64url_encode(sign_ed25519(seed, raw))


@pytest.fixture
async def gfs(tmp_dir: Path) -> AsyncIterator[TestServer]:
    """GFS node "A", listening on a real port, with an admin password."""
    app = create_gfs_app(
        GfsConfig(
            host="127.0.0.1",
            port=0,
            base_url=_UNREACHABLE,
            data_dir=str(tmp_dir),
            instance_id="node-a",
            cluster_enabled=True,
            cluster_node_id="node-a",
            cluster_peers=(),
        )
    )
    server = TestServer(app)
    await server.start_server()
    await app[gfs_admin_repo_key].set_config(
        "admin_password_hash", hash_password("admin-pw")
    )
    try:
        yield server
    finally:
        await server.close()


def _url(server: TestServer) -> str:
    return str(server.make_url("")).rstrip("/")


async def _post(server: TestServer, raw: bytes, sig: str) -> tuple[int, dict]:
    async with ClientSession() as http:
        async with http.post(
            f"{_url(server)}/cluster/sync",
            data=raw,
            headers={"Content-Type": "application/json", "X-Node-Signature": sig},
        ) as resp:
            return resp.status, await resp.json()


async def _admin_add_peer(server: TestServer, body: dict) -> tuple[int, dict]:
    # The admin session cookie is set for a bare IP host.
    async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as http:
        await http.post(f"{_url(server)}/admin/login", json={"password": "admin-pw"})
        async with http.post(
            f"{_url(server)}/admin/api/cluster/peers", json=body
        ) as resp:
            return resp.status, await resp.json()


async def _roster(server: TestServer) -> dict[str, ClusterNode]:
    return {n.node_id: n for n in await server.app[gfs_cluster_repo_key].list_nodes()}


def _own_seed(server: TestServer) -> bytes:
    return server.app[gfs_cluster_key]._signing_key


def _own_key(server: TestServer) -> str:
    return server.app[gfs_cluster_key].own_public_key_hex


async def test_unknown_hello_is_refused_and_writes_nothing(gfs):
    """The old TOFU path: a self-signed HELLO under a fresh key."""
    seed = secrets.token_bytes(32)
    status, body = await _post(
        gfs,
        *_frame(
            seed,
            type_=NODE_HELLO,
            from_node="intruder",
            payload={
                "node_id": "intruder",
                "url": _UNREACHABLE,
                "public_key": ed25519_public_key(seed).hex(),
            },
        ),
    )
    assert (status, body) == (403, {"error": "unapproved_node"})
    # The real sender path fails the same way and also writes nothing.
    intruder = _sender("intruder", seed)
    await intruder.add_peer("node-a", _url(gfs), _own_key(gfs))
    assert await _roster(gfs) == {}
    # And it cannot push state: everything but HELLO needs a row.
    status, body = await _post(
        gfs, *_frame(seed, type_=NODE_HEARTBEAT, from_node="intruder", payload={})
    )
    assert (status, body) == (403, {"error": "unknown_node"})


async def test_shared_seed_node_joins(gfs):
    """A node the operator gave this GFS's identity seed is a member."""
    sibling = _sender("node-b", _own_seed(gfs))
    await sibling.add_peer("node-a", _url(gfs), _own_key(gfs))
    row = (await _roster(gfs))["node-b"]
    assert row.public_key == _own_key(gfs)
    assert row.status == "online"
    # Its sync frames are accepted (``_post_to_peer`` raises on non-2xx).
    await sibling._post_to_peer(_url(gfs), NODE_HEARTBEAT, {"connected_clients": 2})


async def test_admin_pinned_node_with_its_own_key_joins(gfs):
    """A node with a distinct key joins once an operator pins that key."""
    seed = secrets.token_bytes(32)
    key = ed25519_public_key(seed).hex()
    node_c = _sender("node-c", seed)
    status, _ = await _admin_add_peer(
        gfs, {"node_id": "node-c", "url": _UNREACHABLE, "public_key": key}
    )
    assert status == 201
    await node_c.add_peer("node-a", _url(gfs), _own_key(gfs))
    row = (await _roster(gfs))["node-c"]
    assert (row.public_key, row.status) == (key, "online")
    assert row.last_seen is not None
    await node_c._post_to_peer(_url(gfs), NODE_HEARTBEAT, {})


async def test_a_pinned_key_cannot_be_overwritten(gfs):
    seed = secrets.token_bytes(32)
    key = ed25519_public_key(seed).hex()
    await _admin_add_peer(
        gfs, {"node_id": "node-c", "url": _UNREACHABLE, "public_key": key}
    )
    # In-band: a HELLO for node-c under another key — not even our own.
    for other_seed in (secrets.token_bytes(32), _own_seed(gfs)):
        status, body = await _post(
            gfs,
            *_frame(
                other_seed,
                type_=NODE_HELLO,
                from_node="node-c",
                payload={
                    "node_id": "node-c",
                    "url": "http://evil.test",
                    "public_key": ed25519_public_key(other_seed).hex(),
                },
            ),
        )
        assert (status, body) == (403, {"error": "key_mismatch"})
    # Out-of-band: re-adding the node under another key is a conflict.
    status, body = await _admin_add_peer(
        gfs,
        {"node_id": "node-c", "url": _UNREACHABLE, "public_key": _own_key(gfs)},
    )
    assert (status, body) == (409, {"error": "key_mismatch"})
    row = (await _roster(gfs))["node-c"]
    assert (row.public_key, row.url) == (key, _UNREACHABLE)


async def test_a_replayed_frame_is_refused(gfs):
    sibling_seed = _own_seed(gfs)
    await _sender("node-b", sibling_seed).add_peer("node-a", _url(gfs), _own_key(gfs))
    raw, sig = _frame(
        sibling_seed, type_=NODE_HEARTBEAT, from_node="node-b", payload={}
    )
    assert (await _post(gfs, raw, sig))[0] == 200
    assert await _post(gfs, raw, sig) == (409, {"error": "replay"})


@pytest.mark.parametrize("offset", [-(CLUSTER_TS_SKEW_S + 1), CLUSTER_TS_SKEW_S + 1])
async def test_a_stale_frame_is_refused(gfs, offset):
    sibling_seed = _own_seed(gfs)
    await _sender("node-b", sibling_seed).add_peer("node-a", _url(gfs), _own_key(gfs))
    status, body = await _post(
        gfs,
        *_frame(
            sibling_seed,
            type_=NODE_HEARTBEAT,
            from_node="node-b",
            payload={},
            ts=int(time.time()) + offset,
        ),
    )
    assert (status, body) == (401, {"error": "stale_timestamp"})


async def test_an_existing_peer_still_works(gfs):
    """A row first-contact TOFU pinned before the upgrade keeps syncing —
    from a current sender and from one still on the old frame shape."""
    seed = secrets.token_bytes(32)
    key = ed25519_public_key(seed).hex()
    await gfs.app[gfs_cluster_repo_key].upsert_node(
        ClusterNode(
            node_id="old-peer",
            url=_UNREACHABLE,
            public_key=key,
            status="online",
            last_seen="2026-01-01 00:00:00",
        )
    )
    old_peer = _sender("old-peer", seed)
    await old_peer._post_to_peer(
        _url(gfs),
        NODE_HELLO,
        {"node_id": "old-peer", "url": _UNREACHABLE, "public_key": key},
    )
    await old_peer._post_to_peer(_url(gfs), NODE_HEARTBEAT, {})
    status, _ = await _post(
        gfs,
        *_frame(
            seed,
            type_=NODE_HEARTBEAT,
            from_node="old-peer",
            payload={},
            current_shape=False,
        ),
    )
    assert status == 200


# ─── Small-order Ed25519 keys (universal forgeries) ─────────────────────

#: The identity point: OpenSSL accepts it as a key, and the signature
#: ``identity ‖ 0`` then verifies under it for EVERY message.
_IDENTITY_KEY = (1).to_bytes(32, "little")
_FORGED_SIG = b64url_encode((1).to_bytes(32, "little") + bytes(32))

#: Every canonical small-order encoding (see ``tests/test_crypto.py`` for
#: the proof that 8·P is the identity for each).
_P = 2**255 - 19
_Y8 = 2707385501144840649318225287225658788936804267575313519463743609750303402022
_SMALL_ORDER_KEYS = [
    y.to_bytes(32, "little").hex() for y in (1, _P - 1, 0, _Y8, _P - _Y8, _P, _P + 1)
]


@pytest.mark.parametrize("key", _SMALL_ORDER_KEYS)
async def test_admin_cannot_pin_a_small_order_key(gfs, key):
    status, body = await _admin_add_peer(
        gfs, {"node_id": "node-x", "url": _UNREACHABLE, "public_key": key}
    )
    assert (status, body) == (422, {"error": "invalid_public_key"})
    assert await _roster(gfs) == {}


async def test_a_forged_frame_under_a_small_order_pin_is_refused(gfs):
    """Even a small-order pin already on disk (a pre-upgrade TOFU row)
    verifies nothing: the forged policy push is refused, nothing applied."""
    await gfs.app[gfs_cluster_repo_key].upsert_node(
        ClusterNode(
            node_id="forger",
            url=_UNREACHABLE,
            public_key=_IDENTITY_KEY.hex(),
            status="online",
        )
    )
    admin_repo = gfs.app[gfs_admin_repo_key]
    before = await admin_repo.get_config("fraud_threshold")
    raw = json.dumps(
        {
            "type": "NODE_POLICY_PUSH",
            "from": "forger",
            "ts": int(time.time()),
            "nonce": b64url_encode(secrets.token_bytes(16)),
            "sig_suite": "ed25519",
            "payload": {"fraud_threshold": 999999},
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    assert await _post(gfs, raw, _FORGED_SIG) == (401, {"error": "invalid_signature"})
    assert await admin_repo.get_config("fraud_threshold") == before
