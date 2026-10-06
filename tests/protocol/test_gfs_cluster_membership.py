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
  operator gave the node) or a key an operator approved through
  ``POST /admin/api/cluster/peers``;
* an unknown HELLO is refused and writes nothing;
* an approved key is never overwritten in-band;
* a replayed or stale frame is refused;
* membership trusts only ``approved_key`` (written by admin add-peer
  alone, GFS migration 0017) or our own key — never the legacy
  ``public_key`` column, which an old-version node sharing the DB during a
  rolling upgrade keeps writing by TOFU: a shared-seed sibling stays a
  member, a distinct-key peer must be re-added by an operator;
* an approved peer still on the old frame shape keeps syncing.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest
from aiohttp import ClientSession, CookieJar, web
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
from socialhome.global_server.domain import ClusterNode, GfsFraudReport
from socialhome.global_server.server import create_gfs_app

pytestmark = pytest.mark.security

#: A sender's advertised URL nobody listens on: the receiver's reply HELLO
#: fails fast instead of leaving the process.
_UNREACHABLE = "http://127.0.0.1:1"


class _Roster:
    """In-memory roster for a sender-side ``ClusterService``."""

    def __init__(self) -> None:
        self.rows: dict[str, ClusterNode] = {}

    async def approve_node(self, node_id: str, url: str, approved_key: str) -> None:
        self.rows[node_id] = ClusterNode(
            node_id=node_id,
            url=url,
            public_key=approved_key,
            approved_key=approved_key,
            approved_url=url,
        )

    async def insert_node(self, node: ClusterNode) -> None:
        self.rows.setdefault(node.node_id, node)

    async def touch_node(
        self, node_id: str, *, status: str, last_seen: str | None, sibling_url=""
    ) -> None:
        row = self.rows.get(node_id)
        if row is not None:
            moves = bool(sibling_url) and not row.approved_key
            self.rows[node_id] = replace(
                row,
                status=status,
                last_seen=last_seen,
                url=sibling_url if moves else row.url,
            )

    async def reclaim_node(
        self, node_id: str, *, url: str, public_key: str, status: str, last_seen: str
    ) -> None:
        row = self.rows.get(node_id)
        if row is not None and not row.approved_key:
            self.rows[node_id] = replace(
                row, url=url, public_key=public_key, status=status, last_seen=last_seen
            )

    async def list_nodes(self) -> list[ClusterNode]:
        return list(self.rows.values())

    async def remove_node(self, node_id: str) -> None:
        self.rows.pop(node_id, None)

    async def remove_stale_siblings(
        self, *, own_key: str, seen_before: str, keep_node_id: str
    ) -> int:
        return 0

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


async def test_after_upgrade_only_the_shared_seed_sibling_rejoins(gfs):
    """Migration 0017 left every row without an approved key, and kept the
    legacy ``public_key`` — a TOFU pin included. The shared-seed sibling is
    a member through our own key; the peer whose key was TOFU-pinned is
    refused until an operator re-adds it."""
    foreign_seed = secrets.token_bytes(32)
    foreign_key = ed25519_public_key(foreign_seed).hex()
    repo = gfs.app[gfs_cluster_repo_key]
    for node_id, legacy_key in (("node-b", ""), ("old-peer", foreign_key)):
        await repo.insert_node(
            ClusterNode(
                node_id=node_id,
                url=_UNREACHABLE,
                public_key=legacy_key,
                status="online",
                last_seen="2026-01-01 00:00:00",
            )
        )
    sibling = _sender("node-b", _own_seed(gfs))
    await sibling.add_peer("node-a", _url(gfs), _own_key(gfs))
    assert (await _roster(gfs))["node-b"].last_seen != "2026-01-01 00:00:00"
    status, body = await _post(
        gfs,
        *_frame(
            foreign_seed,
            type_=NODE_HELLO,
            from_node="old-peer",
            payload={"node_id": "old-peer", "url": "", "public_key": foreign_key},
        ),
    )
    assert (status, body) == (403, {"error": "unapproved_node"})
    assert await _post(
        gfs,
        *_frame(foreign_seed, type_=NODE_HEARTBEAT, from_node="old-peer", payload={}),
    ) == (403, {"error": "unapproved_node"})
    assert (await _roster(gfs))["old-peer"].approved_key == ""


async def test_shared_seed_node_joins(gfs):
    """A node the operator gave this GFS's identity seed is a member."""
    sibling = _sender("node-b", _own_seed(gfs))
    await sibling.add_peer("node-a", _url(gfs), _own_key(gfs))
    row = (await _roster(gfs))["node-b"]
    # Joined through our own key: nothing was approved for it.
    assert (row.public_key, row.approved_key) == (_own_key(gfs), "")
    assert row.status == "online"
    # Its sync frames are accepted (``_post_to_peer`` raises on non-2xx).
    await sibling._post_to_peer(_url(gfs), NODE_HEARTBEAT, {"connected_clients": 2})


async def test_admin_approved_node_with_its_own_key_joins(gfs):
    """A node with a distinct key joins once an operator approves that key."""
    seed = secrets.token_bytes(32)
    key = ed25519_public_key(seed).hex()
    node_c = _sender("node-c", seed)
    status, _ = await _admin_add_peer(
        gfs, {"node_id": "node-c", "url": _UNREACHABLE, "public_key": key}
    )
    assert status == 201
    await node_c.add_peer("node-a", _url(gfs), _own_key(gfs))
    row = (await _roster(gfs))["node-c"]
    assert (row.approved_key, row.status) == (key, "online")
    assert row.last_seen is not None
    await node_c._post_to_peer(_url(gfs), NODE_HEARTBEAT, {})


async def test_an_approved_key_cannot_be_overwritten(gfs):
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
    assert (row.approved_key, row.url) == (key, _UNREACHABLE)


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


async def test_an_approved_peer_on_the_old_frame_shape_still_works(gfs):
    """An admin-approved peer syncs from a current sender and from one
    still on the old frame shape (no ``nonce`` / ``sig_suite``)."""
    seed = secrets.token_bytes(32)
    key = ed25519_public_key(seed).hex()
    status, _ = await _admin_add_peer(
        gfs, {"node_id": "old-peer", "url": _UNREACHABLE, "public_key": key}
    )
    assert status == 201
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
async def test_admin_cannot_approve_a_small_order_key(gfs, key):
    status, body = await _admin_add_peer(
        gfs, {"node_id": "node-x", "url": _UNREACHABLE, "public_key": key}
    )
    assert (status, body) == (422, {"error": "invalid_public_key"})
    assert await _roster(gfs) == {}


async def test_a_forged_frame_under_a_small_order_key_is_refused(gfs):
    """Even a small-order approved key already on disk (written past the
    admin API's check) verifies nothing: the forged policy push is refused,
    nothing applied."""
    await gfs.app[gfs_cluster_repo_key].approve_node(
        "forger", _UNREACHABLE, _IDENTITY_KEY.hex()
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


# ─── Rolling upgrade on a shared DB: old-version writes grant nothing ─────


def _old_version_write(tmp_dir: Path, sql: str, params: tuple = ()) -> None:
    """A write exactly as a not-yet-upgraded node sharing ``gfs.db`` makes
    it: plain SQL naming only the columns that build knows."""
    conn = sqlite3.connect(tmp_dir / "gfs.db", isolation_level=None, timeout=5)
    try:
        conn.execute(sql, params)
    finally:
        conn.close()


async def test_an_old_version_overwriting_public_key_grants_nothing(gfs, tmp_dir):
    """An old node TOFU-overwrites an approved row's ``public_key`` with an
    attacker's key: the attacker is still no member, the real node still
    is."""
    seed = secrets.token_bytes(32)
    key = ed25519_public_key(seed).hex()
    status, _ = await _admin_add_peer(
        gfs, {"node_id": "node-c", "url": _UNREACHABLE, "public_key": key}
    )
    assert status == 201
    attacker = secrets.token_bytes(32)
    attacker_key = ed25519_public_key(attacker).hex()
    _old_version_write(
        tmp_dir,
        "UPDATE cluster_nodes SET public_key=? WHERE node_id='node-c'",
        (attacker_key,),
    )
    hello = {"node_id": "node-c", "url": "", "public_key": attacker_key}
    assert await _post(
        gfs, *_frame(attacker, type_=NODE_HELLO, from_node="node-c", payload=hello)
    ) == (403, {"error": "key_mismatch"})
    assert await _post(
        gfs, *_frame(attacker, type_=NODE_HEARTBEAT, from_node="node-c", payload={})
    ) == (401, {"error": "invalid_signature"})
    assert await _post(
        gfs, *_frame(seed, type_=NODE_HEARTBEAT, from_node="node-c", payload={})
    ) == (200, {"status": "ok"})


async def test_an_old_version_tofu_row_grants_nothing(gfs, tmp_dir):
    """An old node INSERTs a TOFU row for an attacker's HELLO."""
    attacker = secrets.token_bytes(32)
    attacker_key = ed25519_public_key(attacker).hex()
    _old_version_write(
        tmp_dir,
        "INSERT INTO cluster_nodes(node_id, url, public_key, status, last_seen)"
        " VALUES('tofu', ?, ?, 'online', datetime('now'))",
        (_UNREACHABLE, attacker_key),
    )
    hello = {"node_id": "tofu", "url": "", "public_key": attacker_key}
    assert await _post(
        gfs, *_frame(attacker, type_=NODE_HELLO, from_node="tofu", payload=hello)
    ) == (403, {"error": "unapproved_node"})
    assert await _post(
        gfs, *_frame(attacker, type_=NODE_HEARTBEAT, from_node="tofu", payload={})
    ) == (403, {"error": "unapproved_node"})


# ─── Outbound: a non-member row receives nothing ─────────────────────────


class _Listener:
    """A stub node that records every request it gets."""

    def __init__(self) -> None:
        self.hits: list[tuple[str, str, bytes]] = []
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._record)
        self.server = TestServer(app)

    async def _record(self, request: web.Request) -> web.Response:
        self.hits.append((request.method, request.path, await request.read()))
        if request.path == "/cluster/health":
            return web.json_response({"node_id": "stub", "peers": []})
        return web.json_response({"status": "ok"})

    @property
    def url(self) -> str:
        return _url(self.server)

    def types(self) -> list[str]:
        """The ``type`` of every ``/cluster/sync`` frame received."""
        return [
            json.loads(body)["type"]
            for method, path, body in self.hits
            if method == "POST" and path == "/cluster/sync"
        ]


@pytest.fixture
async def listeners() -> AsyncIterator[tuple[_Listener, _Listener]]:
    """``(member, tofu)``: a shared-seed sibling and an old-version TOFU row."""
    pair = (_Listener(), _Listener())
    for listener in pair:
        await listener.server.start_server()
    try:
        yield pair
    finally:
        for listener in pair:
            await listener.server.close()


async def _member_and_tofu_rows(gfs, tmp_dir, member, tofu) -> None:
    """node-b: a shared-seed sibling (joined through our own key). tofu: a
    row an old-version node inserted for an attacker's self-signed HELLO."""
    await gfs.app[gfs_cluster_repo_key].insert_node(
        ClusterNode(
            node_id="node-b",
            url=member.url,
            public_key=_own_key(gfs),
            status="online",
            last_seen=_recently(),
        )
    )
    attacker_key = ed25519_public_key(secrets.token_bytes(32)).hex()
    _old_version_write(
        tmp_dir,
        "INSERT INTO cluster_nodes(node_id, url, public_key, status, last_seen)"
        " VALUES('tofu', ?, ?, 'online', datetime('now'))",
        (tofu.url, attacker_key),
    )


def _recently() -> str:
    """A ``last_seen`` a live sibling has (naive UTC, like the column)."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _report() -> GfsFraudReport:
    return GfsFraudReport(
        id="r1",
        target_type="space",
        target_id="space-1",
        category="spam",
        notes=None,
        reporter_instance_id="reporter-household",
        reporter_user_id="reporter-user",
        status="pending",
        created_at=int(time.time()),
    )


async def test_a_tofu_row_receives_no_broadcast(gfs, tmp_dir, listeners):
    """A fraud report names its reporter: it must reach members only."""
    member, tofu = listeners
    await _member_and_tofu_rows(gfs, tmp_dir, member, tofu)
    await gfs.app[gfs_cluster_key].sync_report(_report())
    assert member.types() == ["NODE_SYNC_REPORT"]
    assert tofu.hits == []


async def test_a_tofu_row_receives_no_heartbeat(gfs, tmp_dir, listeners):
    member, tofu = listeners
    await _member_and_tofu_rows(gfs, tmp_dir, member, tofu)
    await gfs.app[gfs_cluster_key]._heartbeat_tick()
    assert member.types() == [NODE_HEARTBEAT]
    assert tofu.hits == []


async def test_a_tofu_row_receives_no_partition_catchup(gfs, tmp_dir, listeners):
    member, tofu = listeners
    await _member_and_tofu_rows(gfs, tmp_dir, member, tofu)
    svc = gfs.app[gfs_cluster_key]
    svc.record_relay_ts("space-1")
    for node_id in ("node-b", "tofu"):
        await svc.apply_partition_catchup(node_id, {"space-1": 0})
    assert member.types() == ["NODE_PARTITION_GAP"]
    assert tofu.hits == []


async def test_a_tofu_row_is_never_the_signaling_node(gfs, tmp_dir, listeners):
    member, tofu = listeners
    await _member_and_tofu_rows(gfs, tmp_dir, member, tofu)
    svc = gfs.app[gfs_cluster_key]
    # The idle TOFU row would win the least-connections pick.
    svc._active_sync_count.update({"node-a": 3, "node-b": 2})
    assert await svc.pick_signaling_node() == member.url
    svc._active_sync_count["node-b"] = 4
    assert await svc.pick_signaling_node() == _UNREACHABLE


async def test_a_tofu_row_is_not_listed_on_cluster_health(gfs, tmp_dir, listeners):
    member, tofu = listeners
    await _member_and_tofu_rows(gfs, tmp_dir, member, tofu)
    async with ClientSession() as http:
        async with http.get(f"{_url(gfs)}/cluster/health") as resp:
            body = await resp.json()
    assert [p["node_id"] for p in body["peers"]] == ["node-b"]
    assert tofu.url not in json.dumps(body)


async def test_a_tofu_row_cannot_send_frames_even_under_our_key(
    gfs, tmp_dir, listeners
):
    """Inbound applies the same rule: a row that is not a member takes no
    non-HELLO frame. A real sibling whose row an old version rewrote is a
    member again once its next HELLO — signed under our key — reclaims it."""
    member, tofu = listeners
    await _member_and_tofu_rows(gfs, tmp_dir, member, tofu)
    seed = _own_seed(gfs)
    assert await _post(
        gfs, *_frame(seed, type_=NODE_HEARTBEAT, from_node="tofu", payload={})
    ) == (403, {"error": "unapproved_node"})
    hello = {"node_id": "tofu", "url": member.url, "public_key": _own_key(gfs)}
    assert await _post(
        gfs, *_frame(seed, type_=NODE_HELLO, from_node="tofu", payload=hello)
    ) == (200, {"status": "ok"})
    row = (await _roster(gfs))["tofu"]
    assert (row.public_key, row.url) == (_own_key(gfs), member.url)
    assert await _post(
        gfs, *_frame(seed, type_=NODE_HEARTBEAT, from_node="tofu", payload={})
    ) == (200, {"status": "ok"})


# ─── Outbound URL: an old version cannot redirect a member ───────────────


async def test_an_old_version_url_rewrite_does_not_redirect_an_approved_node(
    gfs, tmp_dir, listeners
):
    """An old node TOFU-accepts an attacker's HELLO for an approved node id
    and upserts ``url`` + ``public_key``: traffic still goes to the URL the
    operator approved, and the approved key still decides inbound."""
    real, attacker_listener = listeners
    seed = secrets.token_bytes(32)
    key = ed25519_public_key(seed).hex()
    status, _ = await _admin_add_peer(
        gfs, {"node_id": "node-c", "url": real.url, "public_key": key}
    )
    assert status == 201
    attacker = secrets.token_bytes(32)
    _old_version_write(
        tmp_dir,
        "UPDATE cluster_nodes SET url=?, public_key=? WHERE node_id='node-c'",
        (attacker_listener.url, ed25519_public_key(attacker).hex()),
    )
    real.hits.clear()
    svc = gfs.app[gfs_cluster_key]
    await svc.sync_report(_report())
    await svc._heartbeat_tick()
    assert real.types() == ["NODE_SYNC_REPORT", NODE_HEARTBEAT]
    assert attacker_listener.hits == []
    nodes = (await svc.admin_cluster())["nodes"]
    assert [n["url"] for n in nodes if n["node_id"] == "node-c"] == [real.url]
    assert await _post(
        gfs, *_frame(attacker, type_=NODE_HEARTBEAT, from_node="node-c", payload={})
    ) == (401, {"error": "invalid_signature"})
    assert await _post(
        gfs, *_frame(seed, type_=NODE_HEARTBEAT, from_node="node-c", payload={})
    ) == (200, {"status": "ok"})


async def test_an_old_version_rewrite_of_a_sibling_row_makes_it_no_member(
    gfs, tmp_dir, listeners
):
    """A shared-seed sibling's row has no approval, so its outbound URL is
    the ``url`` column — which an old node rewrites on a TOFU HELLO. It can
    only do so together with ``public_key`` (the key that HELLO verified
    under, never our own), so the rewritten row is no member: the
    attacker's URL gets nothing."""
    member, attacker_listener = listeners
    await gfs.app[gfs_cluster_repo_key].insert_node(
        ClusterNode(
            node_id="node-b",
            url=member.url,
            public_key=_own_key(gfs),
            status="online",
            last_seen=_recently(),
        )
    )
    attacker = secrets.token_bytes(32)
    _old_version_write(
        tmp_dir,
        "UPDATE cluster_nodes SET url=?, public_key=? WHERE node_id='node-b'",
        (attacker_listener.url, ed25519_public_key(attacker).hex()),
    )
    svc = gfs.app[gfs_cluster_key]
    await svc.sync_report(_report())
    await svc._heartbeat_tick()
    assert member.hits == []
    assert attacker_listener.hits == []
    assert await svc.member_peers() == []
