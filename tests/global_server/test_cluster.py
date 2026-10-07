"""Tests for ClusterService — single-node GFS cluster stub."""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from socialhome.crypto import (
    b64url_decode,
    generate_identity_keypair,
    verify_ed25519,
)
from socialhome.capabilities_sig import (
    CAPS_SIG_SUITE_ED25519,
    verify_capabilities,
)
from socialhome.global_server.cluster import (
    CLUSTER_RATE_LIMIT_PER_MIN,
    CLUSTER_SIG_SUITE_ED25519,
    ClusterReplayCache,
    CLUSTER_TS_SKEW_S,
    SUPPORTED_CLUSTER_SIG_SUITES,
    CLUSTER_UNVERIFIED_RATE_LIMIT_PER_MIN,
    MAX_SIGNALING_SESSIONS,
    NODE_DRAIN_HINT,
    NODE_HEARTBEAT,
    NODE_HELLO,
    NODE_POLICY_PUSH,
    ClusterService,
    FrameVerdict,
    UnsupportedClusterSigSuite,
    authorize_frame,
    is_member,
    member_url,
    parse_cluster_sig_suite,
)
from socialhome.global_server import cluster as cluster_mod
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClusterNode
from socialhome.global_server.repositories import SqliteClusterRepo

# ``cluster_nodes.added_at`` is SQLite's naive ``datetime('now')`` default
# shape — UTC, no ``T``, no zone designator. ``last_seen`` must match it.
_NAIVE_UTC_TS = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


@pytest.fixture
async def cluster(gfs_db):
    """A ClusterService backed by the shared GFS database fixture."""
    repo = SqliteClusterRepo(gfs_db)
    return ClusterService(repo)


@pytest.fixture
async def enabled_cluster(gfs_db):
    """A cluster-mode ClusterService with self pre-announced as node-a."""
    repo = SqliteClusterRepo(gfs_db)
    svc = ClusterService(
        repo,
        node_id="node-a",
        self_url="https://a.gfs.test",
        peers=(),
        own_public_key_hex=_OWN,
        enabled=True,
    )
    # Self row exists so update_active_sync_sessions can target it.
    await repo.insert_node(
        ClusterNode(
            node_id="node-a",
            url="https://a.gfs.test",
            status="online",
        )
    )
    return svc


async def test_list_nodes_empty_initially(cluster):
    """list_nodes() returns an empty list when no nodes have been announced."""
    nodes = await cluster.list_nodes()
    assert nodes == []


def test_no_keyless_registration_entry_point():
    """``announce`` upserted a peer row with no key and no approval; nothing
    called it but tests. Membership only enters through ``add_peer``
    (admin, with a key) or a verified HELLO."""
    assert not hasattr(ClusterService, "announce")


# ─── Spec §24.10.7 — round-robin sync signaling ────────────────────────


async def test_pick_signaling_node_single_node_returns_none(cluster):
    """Cluster mode disabled → return None so the caller omits the field."""
    chosen = await cluster.pick_signaling_node()
    assert chosen is None


async def test_pick_signaling_node_picks_self_when_no_peers(enabled_cluster):
    """Cluster mode enabled, no peers — fall back to self."""
    chosen = await enabled_cluster.pick_signaling_node()
    assert chosen == "https://a.gfs.test"


async def test_pick_signaling_node_picks_least_loaded(enabled_cluster, gfs_db):
    """A peer with a lower active count wins over self."""
    repo = SqliteClusterRepo(gfs_db)
    await repo.insert_node(
        ClusterNode(
            node_id="node-b",
            url="https://b.gfs.test",
            public_key=_OWN,
            status="online",
        )
    )
    await repo.insert_node(
        ClusterNode(
            node_id="node-c",
            url="https://c.gfs.test",
            public_key=_OWN,
            status="online",
        )
    )
    # Self has been used a few times; node-b is hotter; node-c is idle.
    enabled_cluster._active_sync_count["node-a"] = 5
    enabled_cluster._active_sync_count["node-b"] = 9
    enabled_cluster._active_sync_count["node-c"] = 1
    chosen = await enabled_cluster.pick_signaling_node()
    assert chosen == "https://c.gfs.test"


async def test_pick_signaling_node_deterministic_tiebreak(enabled_cluster, gfs_db):
    """Equal counts → break ties by node_id ascending."""
    repo = SqliteClusterRepo(gfs_db)
    await repo.insert_node(
        ClusterNode(
            node_id="node-z", url="https://z.gfs.test", public_key=_OWN, status="online"
        ),
    )
    await repo.insert_node(
        ClusterNode(
            node_id="node-m", url="https://m.gfs.test", public_key=_OWN, status="online"
        ),
    )
    chosen = await enabled_cluster.pick_signaling_node()
    # All three (a, m, z) have count 0 → 'node-a' wins by node_id sort.
    assert chosen == "https://a.gfs.test"


async def test_pick_signaling_node_skips_offline_peers(enabled_cluster, gfs_db):
    """Offline peers are excluded from the candidate set even at zero load."""
    repo = SqliteClusterRepo(gfs_db)
    await repo.insert_node(
        ClusterNode(
            node_id="node-dead",
            url="https://dead.gfs.test",
            status="offline",
        ),
    )
    enabled_cluster._active_sync_count["node-a"] = 100  # load self up
    chosen = await enabled_cluster.pick_signaling_node()
    # node-dead is offline → ignored. self is the only candidate.
    assert chosen == "https://a.gfs.test"


async def test_pick_signaling_node_returns_none_when_all_at_cap(
    enabled_cluster,
    gfs_db,
):
    """Every candidate at MAX_SIGNALING_SESSIONS → None (S-8 reject)."""
    repo = SqliteClusterRepo(gfs_db)
    await repo.insert_node(
        ClusterNode(
            node_id="node-b", url="https://b.gfs.test", public_key=_OWN, status="online"
        ),
    )
    enabled_cluster._active_sync_count["node-a"] = MAX_SIGNALING_SESSIONS
    enabled_cluster._active_sync_count["node-b"] = MAX_SIGNALING_SESSIONS
    chosen = await enabled_cluster.pick_signaling_node()
    assert chosen is None


async def test_note_signaling_started_increments_self_and_persists(
    enabled_cluster,
    gfs_db,
):
    """Self increments live count + writes column for admin UI."""
    await enabled_cluster.note_signaling_started("node-a")
    assert enabled_cluster._active_sync_count["node-a"] == 1
    repo = SqliteClusterRepo(gfs_db)
    nodes = await repo.list_nodes()
    self_row = next(n for n in nodes if n.node_id == "node-a")
    assert self_row.active_sync_sessions == 1


async def test_note_signaling_started_peer_does_not_persist(
    enabled_cluster,
    gfs_db,
):
    """A peer's count moves only in-memory; the DB row is the peer's truth."""
    repo = SqliteClusterRepo(gfs_db)
    await repo.insert_node(
        ClusterNode(
            node_id="node-b", url="https://b.gfs.test", public_key=_OWN, status="online"
        ),
    )
    await enabled_cluster.note_signaling_started("node-b")
    assert enabled_cluster._active_sync_count["node-b"] == 1
    nodes = await repo.list_nodes()
    peer_row = next(n for n in nodes if n.node_id == "node-b")
    assert peer_row.active_sync_sessions == 0  # untouched in DB


async def test_note_signaling_ended_floors_at_zero(enabled_cluster):
    """Idempotent release — repeated calls don't go negative."""
    await enabled_cluster.note_signaling_ended("node-a")
    await enabled_cluster.note_signaling_ended("node-a")
    assert enabled_cluster._active_sync_count["node-a"] == 0


async def test_handle_heartbeat_updates_peer_active_count(
    enabled_cluster,
    gfs_db,
):
    """NODE_HEARTBEAT carries the peer's live count → cluster_nodes row."""
    repo = SqliteClusterRepo(gfs_db)
    await repo.insert_node(
        ClusterNode(
            node_id="node-b", url="https://b.gfs.test", public_key=_OWN, status="online"
        ),
    )
    await enabled_cluster.handle_heartbeat(
        "node-b",
        {"active_sync_sessions": 17},
    )
    assert enabled_cluster._active_sync_count["node-b"] == 17
    nodes = await repo.list_nodes()
    peer_row = next(n for n in nodes if n.node_id == "node-b")
    assert peer_row.active_sync_sessions == 17


async def test_handle_heartbeat_without_payload_is_compat(
    enabled_cluster,
    gfs_db,
):
    """Older peers that omit the count still get a fresh last_seen."""
    repo = SqliteClusterRepo(gfs_db)
    await repo.insert_node(
        ClusterNode(
            node_id="node-b", url="https://b.gfs.test", public_key=_OWN, status="online"
        ),
    )
    await enabled_cluster.handle_heartbeat("node-b", None)
    nodes = await repo.list_nodes()
    peer_row = next(n for n in nodes if n.node_id == "node-b")
    assert peer_row.status == "online"
    assert "node-b" not in enabled_cluster._active_sync_count


async def test_handle_heartbeat_last_seen_matches_added_at_naive_utc_shape(
    enabled_cluster,
    gfs_db,
):
    """``last_seen`` shares ``added_at``'s naive UTC shape.

    Both columns are UTC by the codebase's invariant, but ``last_seen``
    used to be written as a tz-aware ``isoformat()`` string
    (``...+00:00``) while ``added_at`` is SQLite's naive
    ``datetime('now')`` default — a shape mismatch between sibling
    columns on the same row. A reader that correctly treats one shape
    as UTC (e.g. the admin UI's ``normaliseTimestamp``) mishandles the
    other, silently shifting the displayed time by the viewer's UTC
    offset.
    """
    repo = SqliteClusterRepo(gfs_db)
    await repo.insert_node(
        ClusterNode(
            node_id="node-b", url="https://b.gfs.test", public_key=_OWN, status="online"
        ),
    )
    await enabled_cluster.handle_heartbeat("node-b", None)
    nodes = await repo.list_nodes()
    peer_row = next(n for n in nodes if n.node_id == "node-b")
    assert peer_row.last_seen is not None
    assert _NAIVE_UTC_TS.match(peer_row.last_seen), peer_row.last_seen
    assert _NAIVE_UTC_TS.match(peer_row.added_at), peer_row.added_at
    for marker in ("T", "+", "Z"):
        assert marker not in peer_row.last_seen


# ─── connected_clients gossip ─────────────────────────────────────────


class _StubWsRegistry:
    """Minimal ws-registry stub exposing only ``connection_count()``."""

    def __init__(self, count: int) -> None:
        self._count = count

    def connection_count(self) -> int:
        return self._count


async def test_handle_heartbeat_stores_connected_clients(
    enabled_cluster,
    gfs_db,
):
    """NODE_HEARTBEAT carrying connected_clients records it in-memory."""
    repo = SqliteClusterRepo(gfs_db)
    await repo.insert_node(
        ClusterNode(
            node_id="node-b", url="https://b.gfs.test", public_key=_OWN, status="online"
        ),
    )
    await enabled_cluster.handle_heartbeat(
        "node-b",
        {"connected_clients": 42},
    )
    assert enabled_cluster._connected_clients["node-b"] == 42


async def test_handle_heartbeat_missing_connected_clients_no_clobber(
    enabled_cluster,
    gfs_db,
):
    """An older peer omitting connected_clients does not overwrite a prior value."""
    repo = SqliteClusterRepo(gfs_db)
    await repo.insert_node(
        ClusterNode(
            node_id="node-b", url="https://b.gfs.test", public_key=_OWN, status="online"
        ),
    )
    await enabled_cluster.handle_heartbeat("node-b", {"connected_clients": 7})
    assert enabled_cluster._connected_clients["node-b"] == 7
    # Subsequent heartbeat without the key must NOT clobber the stored value.
    await enabled_cluster.handle_heartbeat("node-b", {"active_sync_sessions": 1})
    assert enabled_cluster._connected_clients["node-b"] == 7


async def test_admin_cluster_includes_self_and_peer_counts(gfs_db):
    """admin_cluster() returns all nodes; self carries the live ws count."""
    repo = SqliteClusterRepo(gfs_db)
    svc = ClusterService(
        repo,
        node_id="node-a",
        self_url="https://a.gfs.test",
        peers=(),
        enabled=True,
        ws_registry=_StubWsRegistry(11),
    )
    await repo.insert_node(
        ClusterNode(
            node_id="node-b", url="https://b.gfs.test", public_key=_OWN, status="online"
        ),
    )
    svc._connected_clients["node-b"] = 5
    svc._active_sync_count["node-b"] = 3

    result = await svc.admin_cluster()
    assert result["node_id"] == "node-a"
    assert result["status"] == "online"
    nodes = result["nodes"]
    self_entry = next(n for n in nodes if n["is_self"])
    assert self_entry["node_id"] == "node-a"
    assert self_entry["connected_clients"] == 11
    peer_entry = next(n for n in nodes if n["node_id"] == "node-b")
    assert peer_entry["is_self"] is False
    assert peer_entry["connected_clients"] == 5
    assert peer_entry["active_sync_sessions"] == 3


async def test_admin_cluster_self_with_row_emitted_once(gfs_db):
    """When self already has a cluster_nodes row, it appears exactly once
    and still carries the LIVE ws count (not the persisted row value)."""
    repo = SqliteClusterRepo(gfs_db)
    svc = ClusterService(
        repo,
        node_id="node-a",
        self_url="https://a.gfs.test",
        peers=(),
        enabled=True,
        ws_registry=_StubWsRegistry(11),
    )
    # self is announced into the table (the enabled-cluster case)
    await repo.insert_node(
        ClusterNode(node_id="node-a", url="https://a.gfs.test", status="online"),
    )
    await repo.insert_node(
        ClusterNode(
            node_id="node-b", url="https://b.gfs.test", public_key=_OWN, status="online"
        ),
    )

    result = await svc.admin_cluster()
    nodes = result["nodes"]
    self_entries = [n for n in nodes if n["is_self"]]
    assert len(self_entries) == 1
    assert self_entries[0]["node_id"] == "node-a"
    # live ws count, not the row's default of 0
    assert self_entries[0]["connected_clients"] == 11


# ─── Spec §4.4.6 — partition catchup ──────────────────────────────────


async def test_record_relay_ts_bumps_high_water_mark(enabled_cluster):
    enabled_cluster.record_relay_ts("space-1")
    assert "space-1" in enabled_cluster._local_last_relay_ts


async def test_apply_relay_records_local_ts(enabled_cluster):
    """Inbound NODE_RELAY also bumps the per-space timestamp."""
    await enabled_cluster.apply_relay(
        "space-1",
        {"msg_id": "m1", "encrypted_payload": "..."},
    )
    assert "space-1" in enabled_cluster._local_last_relay_ts


async def test_apply_partition_catchup_returns_gaps_for_newer_local(
    enabled_cluster,
):
    """Peer's last_relay_ts older than ours → emit a gap descriptor."""
    enabled_cluster._local_last_relay_ts["sp-1"] = 1000.0
    enabled_cluster._local_last_relay_ts["sp-2"] = 500.0
    enabled_cluster._enabled = False  # skip outbound POST in this unit test
    gaps = await enabled_cluster.apply_partition_catchup(
        "node-b",
        {"sp-1": 800.0, "sp-2": 600.0},
    )
    # sp-1 is newer locally → gap; sp-2 is newer on peer → no gap from us.
    assert len(gaps) == 1
    assert gaps[0]["space_id"] == "sp-1"
    assert gaps[0]["gap_start"] == 800.0
    assert gaps[0]["gap_end"] == 1000.0


async def test_apply_partition_catchup_handles_unknown_spaces(enabled_cluster):
    """Spaces we never relayed (local_ts=0) → no gap reported."""
    enabled_cluster._enabled = False
    gaps = await enabled_cluster.apply_partition_catchup(
        "node-b",
        {"never-seen": 1000.0},
    )
    assert gaps == []


async def test_apply_partition_catchup_ignores_malformed_payload(enabled_cluster):
    enabled_cluster._enabled = False
    assert await enabled_cluster.apply_partition_catchup("node-b", "garbage") == []
    assert await enabled_cluster.apply_partition_catchup("node-b", None) == []


async def test_apply_partition_gap_records_for_drain(enabled_cluster):
    await enabled_cluster.apply_partition_gap(
        {"space_id": "sp-1", "gap_start": 100.0, "gap_end": 200.0},
    )
    pending = enabled_cluster.pending_partition_gaps()
    assert pending == [
        {"space_id": "sp-1", "gap_start": 100.0, "gap_end": 200.0},
    ]
    # Drain is destructive — second call returns empty.
    assert enabled_cluster.pending_partition_gaps() == []


async def test_apply_partition_gap_drops_invalid_range(enabled_cluster):
    """gap_end <= gap_start is bogus — silently discarded."""
    await enabled_cluster.apply_partition_gap(
        {"space_id": "sp-1", "gap_start": 200.0, "gap_end": 100.0},
    )
    assert enabled_cluster.pending_partition_gaps() == []


# ─── Heartbeat scheduler lifecycle ────────────────────────────────────────


async def test_heartbeat_start_then_stop_exits_via_stop_event(enabled_cluster):
    """``stop()`` must drain the heartbeat loop via ``_stop``, not bare cancel."""
    await enabled_cluster.start()
    task = enabled_cluster._heartbeat_task
    assert task is not None and not task.done()
    await enabled_cluster.stop()
    assert enabled_cluster._heartbeat_task is None
    # Task exited cleanly via the stop event — it should not be cancelled.
    assert task.done() and not task.cancelled()


async def test_heartbeat_start_is_idempotent(enabled_cluster):
    """A second ``start()`` while the first task is alive is a no-op."""
    await enabled_cluster.start()
    first_task = enabled_cluster._heartbeat_task
    await enabled_cluster.start()
    assert enabled_cluster._heartbeat_task is first_task
    await enabled_cluster.stop()


async def test_sign_capabilities_block_signs_with_the_published_identity(gfs_db):
    """The capability block ``GET /gfs/info`` serves is signed with the SAME
    key whose public half that response publishes (and every household pins at
    pair time) — no capability-specific key is minted, and the seed never
    leaves this service."""
    kp = generate_identity_keypair()
    svc = ClusterService(
        SqliteClusterRepo(gfs_db),
        node_id="node-a",
        signing_key=kp.private_key,
        own_public_key_hex=kp.public_key.hex(),
    )
    caps = {"anonymous_publish": True}
    sig, suite = svc.sign_capabilities_block("gfs-1", caps)
    assert suite == CAPS_SIG_SUITE_ED25519
    assert verify_capabilities(svc.own_public_key_hex, "gfs-1", caps, sig, suite)


async def test_sign_capabilities_block_without_a_key_signs_nothing(cluster):
    """No identity wired → no signature rather than an unverifiable one; the
    route then omits the block and households keep the legacy relay body."""
    assert cluster.sign_capabilities_block("gfs-1", {"anonymous_publish": True}) == (
        "",
        "",
    )


async def test_handle_hello_replies_on_first_contact_so_discovery_is_bidirectional(
    enabled_cluster, monkeypatch
):
    """A HELLO from an unknown peer is answered with our own HELLO.

    ``_announce_to_peers`` only fires once at startup, so a node whose peer was
    down at that instant would otherwise never be re-announced to — a cold-start
    deadlock where one node stays ``unknown_node`` (403) to the other forever.
    Replying on first contact converges both sides the moment either announces.
    """
    posted: list[tuple] = []

    async def fake_post(self, url, msg_type, payload, *, to="", session=None):
        posted.append((url, msg_type, payload))

    # ClusterService is __slots__-ed, so patch the class method, not the
    # instance.
    monkeypatch.setattr(ClusterService, "_post_to_peer", fake_post)

    # First contact from an unknown shared-seed sibling → we register it
    # AND HELLO back.
    await enabled_cluster.handle_hello(
        from_node_id="node-b",
        url="https://b.gfs.test",
        public_key_hex=_OWN,
    )
    node_ids = {n.node_id for n in await enabled_cluster.list_nodes()}
    assert "node-b" in node_ids
    replies = [p for p in posted if p[1] == NODE_HELLO]
    assert len(replies) == 1, posted
    assert replies[0][0] == "https://b.gfs.test"
    assert replies[0][2]["node_id"] == "node-a"

    # A SECOND hello from the now-known peer must NOT reply — no ping-pong.
    posted.clear()
    await enabled_cluster.handle_hello(
        from_node_id="node-b",
        url="https://b.gfs.test",
        public_key_hex=_OWN,
    )
    assert [p for p in posted if p[1] == NODE_HELLO] == []


async def test_handle_hello_ignores_a_message_from_this_node_itself(
    enabled_cluster, monkeypatch
):
    """The Nomad peer list includes this alloc, so a node HELLOs itself.
    Registering self would inflate cluster_nodes and make a node peer with
    itself — ignore a hello whose from_node_id is our own.
    """
    posted: list = []

    async def fake_post(self, url, msg_type, payload, *, to="", session=None):
        posted.append((url, msg_type))

    monkeypatch.setattr(ClusterService, "_post_to_peer", fake_post)

    before = {n.node_id for n in await enabled_cluster.list_nodes()}
    await enabled_cluster.handle_hello(
        from_node_id="node-a",  # == enabled_cluster's own node_id
        url="https://a.gfs.test",
        public_key_hex="aa" * 32,
    )
    after = {n.node_id for n in await enabled_cluster.list_nodes()}
    # No new node, and no reply-hello to ourselves.
    assert after == before
    assert posted == []


# ─── /cluster/sync budgets (injected clock) ──────────────────────────


class _Clock:
    def __init__(self) -> None:
        self.now = 5000.0

    def __call__(self) -> float:
        return self.now


async def test_verified_sync_budget_is_per_node_and_slides(gfs_db):
    clock = _Clock()
    svc = ClusterService(SqliteClusterRepo(gfs_db), clock=clock)
    for _ in range(CLUSTER_RATE_LIMIT_PER_MIN):
        assert svc.charge_verified_sync("node-b")
    assert not svc.charge_verified_sync("node-b")
    assert svc.charge_verified_sync("node-c")
    clock.now += 60.5
    assert svc.charge_verified_sync("node-b")


async def test_unverified_sync_budget_gates_the_address_read_only(gfs_db):
    clock = _Clock()
    svc = ClusterService(SqliteClusterRepo(gfs_db), clock=clock)
    # Looking never spends: an address with no failures is never shed.
    for _ in range(CLUSTER_UNVERIFIED_RATE_LIMIT_PER_MIN * 2):
        assert not svc.sync_source_exhausted("203.0.113.9")
    for _ in range(CLUSTER_UNVERIFIED_RATE_LIMIT_PER_MIN):
        assert svc.charge_unverified_sync("203.0.113.9")
    assert svc.sync_source_exhausted("203.0.113.9")
    assert not svc.charge_unverified_sync("203.0.113.9")
    assert not svc.sync_source_exhausted("198.51.100.7")
    # The two budgets are independent: a node name is not an address.
    assert svc.charge_verified_sync("203.0.113.9")
    clock.now += 60.5
    assert not svc.sync_source_exhausted("203.0.113.9")


# ─── Frame wire shape: nonce + sig_suite ─────────────────────────────


class _FakeResp:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self) -> None:
        self.posts: list[tuple[str, bytes, dict]] = []

    def post(self, url, *, data, headers, **_kw):
        self.posts.append((url, data, headers))
        return _FakeResp()


async def test_outbound_frame_carries_nonce_suite_and_wall_clock_ts(gfs_db):
    """Each frame is unique (16-byte nonce), names its signature suite, takes
    ``ts`` from the injected wall clock, and is signed over the exact bytes
    sent — with no unsigned sender-id header."""
    kp = generate_identity_keypair()
    svc = ClusterService(
        SqliteClusterRepo(gfs_db),
        node_id="node-a",
        signing_key=kp.private_key,
        own_public_key_hex=kp.public_key.hex(),
        wall_clock=lambda: 1_900_000_000.7,
    )
    session = _FakeSession()
    for _ in range(2):
        await svc._post_to_peer("https://b.test", NODE_HEARTBEAT, {}, session=session)
    (url, raw, headers), (_, raw2, _) = session.posts
    assert url == "https://b.test/cluster/sync"
    body = json.loads(raw)
    assert body["from"] == "node-a"
    assert body["ts"] == 1_900_000_000
    assert body["sig_suite"] == CLUSTER_SIG_SUITE_ED25519
    assert len(b64url_decode(body["nonce"])) == 16
    assert json.loads(raw2)["nonce"] != body["nonce"]
    assert "X-Node-Id" not in headers
    assert verify_ed25519(
        kp.public_key, raw, b64url_decode(headers["X-Node-Signature"])
    )


async def test_outbound_frame_binds_its_recipient_when_known(gfs_db):
    """``to`` is signed into the body when the caller knows the recipient's
    node id, and left out otherwise (a HELLO to a configured URL)."""
    kp = generate_identity_keypair()
    svc = ClusterService(
        SqliteClusterRepo(gfs_db),
        node_id="node-a",
        signing_key=kp.private_key,
        own_public_key_hex=kp.public_key.hex(),
    )
    session = _FakeSession()
    await svc._post_to_peer(
        "https://b.test", NODE_HEARTBEAT, {}, to="node-b", session=session
    )
    await svc._post_to_peer("https://b.test", NODE_HELLO, {}, session=session)
    (_, raw, headers), (_, raw2, _) = session.posts
    assert json.loads(raw)["to"] == "node-b"
    assert verify_ed25519(
        kp.public_key, raw, b64url_decode(headers["X-Node-Signature"])
    )
    assert "to" not in json.loads(raw2)


async def test_every_known_peer_send_names_its_recipient(enabled_cluster, monkeypatch):
    """Fan-out, HELLO replies, admin add-peer and re-announces to a URL we
    already have a row for all name the recipient node id."""
    posted: list[tuple[str, str, str]] = []

    async def fake_post(self, url, msg_type, payload, *, to="", session=None):
        posted.append((url, msg_type, to))

    monkeypatch.setattr(ClusterService, "_post_to_peer", fake_post)
    repo = enabled_cluster._repo
    await repo.insert_node(
        ClusterNode(
            node_id="node-b",
            url="https://b.gfs.test",
            public_key=_OWN,
            status="online",
        )
    )
    await enabled_cluster.sync_policy({"fraud_threshold": 3})
    assert ("https://b.gfs.test", NODE_POLICY_PUSH, "node-b") in posted
    assert all(to for _, t, to in posted if t == NODE_POLICY_PUSH)
    posted.clear()
    await enabled_cluster.handle_hello(
        from_node_id="node-c", url="https://c.gfs.test", public_key_hex=_OWN
    )
    assert posted == [("https://c.gfs.test", NODE_HELLO, "node-c")]
    posted.clear()
    await enabled_cluster.add_peer("node-d", "https://d.gfs.test", _PIN)
    assert posted == [("https://d.gfs.test", NODE_HELLO, "node-d")]
    posted.clear()
    enabled_cluster._peers = ("https://b.gfs.test", "https://new.gfs.test")
    await enabled_cluster._announce_to_peers()
    assert posted == [
        ("https://b.gfs.test", NODE_HELLO, "node-b"),
        ("https://new.gfs.test", NODE_HELLO, ""),
    ]


def test_cluster_sig_suite_parse():
    assert SUPPORTED_CLUSTER_SIG_SUITES == frozenset({CLUSTER_SIG_SUITE_ED25519})
    # An older sender ships no suite: it can only have been ed25519.
    assert parse_cluster_sig_suite(None) == CLUSTER_SIG_SUITE_ED25519
    assert parse_cluster_sig_suite("ed25519") == CLUSTER_SIG_SUITE_ED25519
    for bad in ("ed25519+mldsa65", "ED25519", "", 1, ["ed25519"]):
        with pytest.raises(UnsupportedClusterSigSuite):
            parse_cluster_sig_suite(bad)
    assert issubclass(UnsupportedClusterSigSuite, ValueError)


# ─── Membership rule (authorize_frame) ───────────────────────────────

_OWN = "a1" * 32
_PIN = "b2" * 32
_OTHER = "c3" * 32


def _row(key: str, *, legacy_public_key: str = "") -> ClusterNode:
    return ClusterNode(
        node_id="node-b",
        url="https://b.test",
        approved_key=key,
        public_key=legacy_public_key,
    )


def _hello(carried: str, pinned: ClusterNode | None, own: str = _OWN):
    return authorize_frame(
        msg_type=NODE_HELLO,
        from_node="node-b",
        carried_key=carried,
        row=pinned,
        own_key=own,
    )


def test_hello_under_our_own_key_is_a_member():
    """Shared seed: holding our identity key IS the operator's approval."""
    assert _hello(_OWN, None) == FrameVerdict(verify_key=_OWN)
    assert _hello(_OWN.upper(), None) == FrameVerdict(verify_key=_OWN)
    # An empty pin (a row with no key yet) is filled by the shared seed.
    assert _hello(_OWN, _row("")) == FrameVerdict(verify_key=_OWN)


def test_hello_under_the_admin_pinned_key_is_a_member():
    assert _hello(_PIN, _row(_PIN)) == FrameVerdict(verify_key=_PIN)


def test_hello_under_an_unknown_key_is_unapproved():
    assert _hello(_OTHER, None) == FrameVerdict(error="unapproved_node")
    assert _hello(_OTHER, _row("")) == FrameVerdict(error="unapproved_node")
    assert _hello("", None) == FrameVerdict(error="unapproved_node")
    # No identity configured: nothing is our own key.
    assert _hello("", None, own="") == FrameVerdict(error="unapproved_node")


def test_hello_never_moves_a_pin():
    """A known node id with a different key is refused — even our own key:
    rotation is delete then re-add, never an in-band swap."""
    assert _hello(_OTHER, _row(_PIN)) == FrameVerdict(error="key_mismatch")
    assert _hello(_OWN, _row(_PIN)) == FrameVerdict(error="key_mismatch")


def test_non_hello_verifies_under_the_pin_or_our_own_key():
    def _hb(pinned, own=_OWN):
        return authorize_frame(
            msg_type=NODE_HEARTBEAT,
            from_node="node-b",
            carried_key="",
            row=pinned,
            own_key=own,
        )

    assert _hb(None) == FrameVerdict(error="unknown_node")
    assert _hb(_row(_PIN)) == FrameVerdict(verify_key=_PIN)
    # A shared-seed sibling's row holds our own key (a HELLO under it
    # created the row) and verifies under it.
    assert _hb(_row("", legacy_public_key=_OWN)) == FrameVerdict(verify_key=_OWN)
    # A row with neither is no member (:func:`is_member`).
    assert _hb(_row("")) == FrameVerdict(error="unapproved_node")
    assert _hb(_row(""), own="") == FrameVerdict(error="unapproved_node")


@pytest.mark.security
def test_the_legacy_public_key_column_is_never_trusted():
    """``public_key`` is written by trust-on-first-use in older builds (and
    by old-version nodes sharing the DB): it must grant nothing."""
    legacy = _row("", legacy_public_key=_OTHER)
    assert _hello(_OTHER, legacy) == FrameVerdict(error="unapproved_node")
    assert authorize_frame(
        msg_type=NODE_HEARTBEAT,
        from_node="node-b",
        carried_key="",
        row=legacy,
        own_key=_OWN,
    ) == FrameVerdict(error="unapproved_node")
    # An approved row whose public_key an old node overwrote.
    overwritten = _row(_PIN, legacy_public_key=_OTHER)
    assert _hello(_OTHER, overwritten) == FrameVerdict(error="key_mismatch")
    assert _hello(_PIN, overwritten) == FrameVerdict(verify_key=_PIN)


def test_is_member_needs_an_approval_or_our_own_key():
    """The one membership rule, shared by inbound and outbound."""
    assert is_member(_row(_PIN), _OWN)
    assert is_member(_row(_OWN), _OWN)
    assert is_member(_row("", legacy_public_key=_OWN), _OWN)
    assert is_member(_row("", legacy_public_key=_OWN.upper()), _OWN)
    # A TOFU row an old-version node wrote, a keyless row, no identity.
    assert not is_member(_row("", legacy_public_key=_OTHER), _OWN)
    assert not is_member(_row(""), _OWN)
    assert not is_member(_row(""), "")
    assert is_member(_row(_PIN), "")


# ─── key_source in the admin view ────────────────────────────────────


async def _svc_with_rows(gfs_db, *, enabled: bool = True) -> ClusterService:
    repo = SqliteClusterRepo(gfs_db)
    svc = ClusterService(
        repo,
        node_id="node-a",
        self_url="https://a.gfs.test",
        own_public_key_hex=_OWN,
        enabled=enabled,
    )
    # A shared-seed sibling's HELLO row, and legacy rows from before
    # approvals (a TOFU key, no key).
    for node_id, key in (("sibling", _OWN), ("legacy-tofu", _OTHER), ("blank", "")):
        await repo.insert_node(
            ClusterNode(node_id=node_id, url=f"https://{node_id}.test", public_key=key)
        )
    await repo.approve_node("admin-added", "https://admin-added.test", _PIN)
    await repo.approve_node("approved-own", "https://approved-own.test", _OWN)
    return svc


async def test_admin_cluster_marks_each_node_key_source(gfs_db):
    """``approved`` = an admin-approved key; ``own`` = our identity key
    (shared seed); ``none`` = neither — a row from before approvals that a
    peer with its own key needs re-adding for."""
    svc = await _svc_with_rows(gfs_db)
    nodes = {n["node_id"]: n for n in (await svc.admin_cluster())["nodes"]}
    view = {k: (n["key_source"], n["public_key"]) for k, n in nodes.items()}
    assert view == {
        "node-a": ("own", _OWN),
        "sibling": ("own", _OWN),
        "approved-own": ("own", _OWN),
        "admin-added": ("approved", _PIN),
        # The legacy TOFU key is not shown as the node's key.
        "legacy-tofu": ("none", ""),
        "blank": ("none", ""),
    }


async def test_startup_does_not_warn_about_pinned_peers(gfs_db, caplog):
    """Every pin is admin-approved now (migration 0017 cleared the older
    ones), so start-up has nothing to flag."""
    svc = await _svc_with_rows(gfs_db, enabled=False)
    with caplog.at_level("WARNING"):
        await svc.start()
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


# ─── Replay window vs. freshness window (injected clocks) ─────────────


class _Clocks:
    """A monotonic and a wall clock the test moves independently."""

    def __init__(self, wall: float = 1_900_000_000.0) -> None:
        self.mono = 1000.0
        self.wall = wall

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += seconds


def _replay_svc(gfs_db, clocks: _Clocks) -> ClusterService:
    return ClusterService(
        SqliteClusterRepo(gfs_db),
        node_id="node-a",
        clock=lambda: clocks.mono,
        wall_clock=lambda: clocks.wall,
    )


def _accept(svc: ClusterService, raw: bytes, ts: int) -> bool:
    """Mirror the route: fresh and not seen → record and accept."""
    if svc.frame_ts_error(ts) or svc.frame_seen(raw, ts):
        return False
    return svc.record_frame(raw, ts, "node-b")


async def test_a_future_dated_frame_is_not_replayable_while_still_fresh(gfs_db):
    """``ts = now + 300`` is fresh until wall ``now + 600``; the digest must
    be remembered at least that long."""
    clocks = _Clocks()
    svc = _replay_svc(gfs_db, clocks)
    ts = int(clocks.wall) + CLUSTER_TS_SKEW_S
    raw = b'{"frame":"future"}'
    assert _accept(svc, raw, ts)
    for _ in range(2 * CLUSTER_TS_SKEW_S + 5):
        clocks.advance(1)
        assert not _accept(svc, raw, ts), clocks.wall - ts


async def test_a_wall_clock_step_back_does_not_reopen_the_replay_window(gfs_db):
    """Monotonic time runs on while the wall clock steps back 60 s: the
    frame is fresh again by the wall clock, so it must still be seen."""
    clocks = _Clocks()
    svc = _replay_svc(gfs_db, clocks)
    ts = int(clocks.wall) + CLUSTER_TS_SKEW_S
    raw = b'{"frame":"stepped"}'
    assert _accept(svc, raw, ts)
    clocks.advance(2 * CLUSTER_TS_SKEW_S - 10)
    clocks.wall -= 60
    assert svc.frame_ts_error(ts) == ""  # fresh again by the wall clock …
    assert not _accept(svc, raw, ts)  # … and still refused as a replay
    for _ in range(200):
        clocks.advance(1)
        assert not _accept(svc, raw, ts)


async def test_a_replay_entry_expires_only_once_its_frame_is_stale(gfs_db):
    """For any ``ts`` in the window: whenever the cache has forgotten the
    frame, the frame is already stale — under either clock moving."""
    clocks = _Clocks()
    svc = _replay_svc(gfs_db, clocks)
    clocks.advance(CLUSTER_TS_SKEW_S)  # past the boot floor
    base = int(clocks.wall)
    offsets = range(-CLUSTER_TS_SKEW_S, CLUSTER_TS_SKEW_S + 1, 37)
    for offset in offsets:
        assert _accept(svc, f"frame-{offset}".encode(), base + offset)
    for step in range(3 * CLUSTER_TS_SKEW_S):
        clocks.advance(1)
        if step == CLUSTER_TS_SKEW_S:
            clocks.wall -= 60  # a wall-clock step back mid-way
        for offset in offsets:
            raw = f"frame-{offset}".encode()
            assert svc.frame_seen(raw, base + offset) or svc.frame_ts_error(
                base + offset
            )


@pytest.mark.security
def test_a_full_replay_cache_refuses_new_frames_and_keeps_its_floor():
    """At capacity the NEW frame is refused; no live digest is evicted and
    the floor does not move, so nothing already accepted can replay and
    nothing fresh is mistaken for a replay."""
    cache = ClusterReplayCache(cap=3, per_node_cap=10)
    now = 1_900_000_000.0
    for i, ts in enumerate((100, 50, 300)):
        assert cache.record(f"d{i}".encode(), int(now) + ts, f"n{i}", now=now)
    assert not cache.record(b"d3", int(now) + 200, "n3", now=now)
    assert len(cache) == 3
    assert cache.floor is None
    assert not cache.seen(b"d3", int(now) + 200, now=now)
    for i, ts in ((0, 100), (1, 50), (2, 300)):
        assert cache.seen(f"d{i}".encode(), int(now) + ts, now=now)
    # A digest already held is a no-op, not a refusal.
    assert cache.record(b"d0", int(now) + 100, "n0", now=now)


@pytest.mark.security
def test_one_node_cannot_crowd_another_out_of_the_replay_cache():
    cache = ClusterReplayCache(cap=100, per_node_cap=2)
    now = 1_900_000_000.0
    assert cache.record(b"a1", int(now), "a", now=now)
    assert cache.record(b"a2", int(now), "a", now=now)
    assert not cache.record(b"a3", int(now), "a", now=now)
    assert cache.record(b"b1", int(now), "b", now=now)
    # Once a's entries expire, its share frees up again.
    later = now + 2 * CLUSTER_TS_SKEW_S
    assert cache.record(b"a3", int(later), "a", now=later)


def test_replay_cache_expires_on_the_frames_ts_not_insertion_order():
    cache = ClusterReplayCache(cap=10, per_node_cap=10)
    now = 1_900_000_000.0
    cache.record(b"late", int(now) + 300, "n", now=now)
    cache.record(b"early", int(now) - 290, "n", now=now)
    # ``early`` is stale at now + 11 and forgotten; ``late`` is kept.
    later = now + 12
    assert cache.seen(b"late", int(now) + 300, now=later)
    assert cache.seen(b"early", int(now) - 290, now=later)  # via the floor
    assert len(cache) == 1
    assert cache.floor == int(now) - 290


# ─── In-band URL updates (HELLO) ─────────────────────────────────────


@pytest.fixture
def hello_replies(monkeypatch):
    sent: list[str] = []

    async def fake_post(self, url, msg_type, payload, *, to="", session=None):
        sent.append(url)

    monkeypatch.setattr(ClusterService, "_post_to_peer", fake_post)
    return sent


async def _node_row(svc: ClusterService, node_id: str) -> ClusterNode:
    (row,) = [n for n in await svc.list_nodes() if n.node_id == node_id]
    return row


@pytest.mark.security
async def test_hello_never_overwrites_a_known_url(enabled_cluster, hello_replies):
    """The admin's URL wins: a member's HELLO cannot point its row (and so
    every later heartbeat and fan-out POST) at another address."""
    await enabled_cluster.add_peer("node-b", "https://b.gfs.test", _PIN)
    hello_replies.clear()
    await enabled_cluster.handle_hello(
        from_node_id="node-b",
        url="http://169.254.169.254/latest/meta-data",
        public_key_hex=_PIN,
    )
    assert (await _node_row(enabled_cluster, "node-b")).url == "https://b.gfs.test"
    assert hello_replies == ["https://b.gfs.test"]


@pytest.mark.parametrize(
    "bad",
    [
        "ftp://c.gfs.test",
        "http://",
        "http://user:pw@c.gfs.test",
        "javascript:alert(1)",
        "https://c.gfs.test/?q=1",
        "https://c.gfs.test/#frag",
        "c.gfs.test",
    ],
)
async def test_first_hello_with_an_invalid_url_stores_none_and_sends_nothing(
    enabled_cluster, hello_replies, bad
):
    await enabled_cluster.handle_hello(
        from_node_id="node-c", url=bad, public_key_hex=_OWN
    )
    assert (await _node_row(enabled_cluster, "node-c")).url == ""
    assert hello_replies == []


async def test_first_hello_with_a_valid_url_records_it(enabled_cluster, hello_replies):
    await enabled_cluster.handle_hello(
        from_node_id="node-c", url="https://c.gfs.test/", public_key_hex=_OWN
    )
    assert (await _node_row(enabled_cluster, "node-c")).url == "https://c.gfs.test"
    assert hello_replies == ["https://c.gfs.test"]


@pytest.mark.security
async def test_sibling_hello_refreshes_its_url(enabled_cluster, hello_replies):
    """A shared-seed sibling's HELLO verified under our OWN key — the sender
    is this GFS — so its URL follows the HELLO: a redeployed alloc on a new
    port is reached there, not at the dead old one. No second reply."""
    await enabled_cluster.handle_hello(
        from_node_id="node-c", url="https://c.gfs.test:1111", public_key_hex=_OWN
    )
    hello_replies.clear()
    await enabled_cluster.handle_hello(
        from_node_id="node-c", url="https://c.gfs.test:2222/", public_key_hex=_OWN
    )
    assert (await _node_row(enabled_cluster, "node-c")).url == "https://c.gfs.test:2222"
    assert hello_replies == []


@pytest.mark.security
@pytest.mark.parametrize(
    "bad", ["http://169.254.169.254/x", "javascript:alert(1)", "c.gfs.test"]
)
async def test_sibling_hello_with_an_unusable_url_keeps_the_old_one(
    enabled_cluster, hello_replies, bad
):
    await enabled_cluster.handle_hello(
        from_node_id="node-c", url="https://c.gfs.test:1111", public_key_hex=_OWN
    )
    await enabled_cluster.handle_hello(
        from_node_id="node-c", url=bad, public_key_hex=_OWN
    )
    assert (await _node_row(enabled_cluster, "node-c")).url == "https://c.gfs.test:1111"


@pytest.mark.security
async def test_sibling_heartbeat_refreshes_its_url(enabled_cluster, hello_replies):
    """A heartbeat carries the sender's URL and always names its recipient,
    so a sibling's row converges on its current URL within one heartbeat
    interval — even if a to-less HELLO replayed elsewhere rolled it back."""
    await enabled_cluster.handle_hello(
        from_node_id="node-c", url="https://c.gfs.test:1111", public_key_hex=_OWN
    )
    await enabled_cluster.handle_heartbeat(
        "node-c", {"url": "https://c.gfs.test:2222/", "active_sync_sessions": 0}
    )
    assert (await _node_row(enabled_cluster, "node-c")).url == "https://c.gfs.test:2222"


@pytest.mark.security
async def test_approved_node_heartbeat_never_moves_its_url(
    enabled_cluster, hello_replies
):
    await enabled_cluster.add_peer("node-b", "https://b.gfs.test", _PIN)
    await enabled_cluster.handle_heartbeat(
        "node-b", {"url": "https://evil.gfs.test", "active_sync_sessions": 0}
    )
    row = await _node_row(enabled_cluster, "node-b")
    assert (row.url, member_url(row)) == ("https://b.gfs.test", "https://b.gfs.test")


async def test_heartbeat_without_url_keeps_the_url(enabled_cluster, hello_replies):
    """An older peer's heartbeat carries no ``url`` — the row keeps its URL."""
    await enabled_cluster.handle_hello(
        from_node_id="node-c", url="https://c.gfs.test:1111", public_key_hex=_OWN
    )
    await enabled_cluster.handle_heartbeat("node-c", {"active_sync_sessions": 0})
    assert (await _node_row(enabled_cluster, "node-c")).url == "https://c.gfs.test:1111"


@pytest.mark.security
async def test_sibling_hello_to_our_own_url_is_refused_and_warned(
    enabled_cluster, hello_replies, caplog
):
    """Two allocs rendering the same ``advertise_url``: a sibling announcing
    OUR URL would make us HELLO and heartbeat ourselves. Refused, and the
    operator sees one WARNING per node, not one per heartbeat."""
    await enabled_cluster.handle_hello(
        from_node_id="node-c", url="https://c.gfs.test:1111", public_key_hex=_OWN
    )
    with caplog.at_level("WARNING"):
        await enabled_cluster.handle_hello(
            from_node_id="node-c", url="https://a.gfs.test/", public_key_hex=_OWN
        )
        await enabled_cluster.handle_heartbeat(
            "node-c", {"url": "https://a.gfs.test", "active_sync_sessions": 0}
        )
    assert (await _node_row(enabled_cluster, "node-c")).url == "https://c.gfs.test:1111"
    warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "advertise_url" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "node-c" in warnings[0].getMessage()


@pytest.mark.security
async def test_hello_under_a_foreign_key_never_moves_a_sibling_url(
    enabled_cluster, hello_replies
):
    await enabled_cluster.handle_hello(
        from_node_id="node-c", url="https://c.gfs.test:1111", public_key_hex=_OWN
    )
    await enabled_cluster.handle_hello(
        from_node_id="node-c", url="https://evil.gfs.test", public_key_hex=_PIN
    )
    assert (await _node_row(enabled_cluster, "node-c")).url == "https://c.gfs.test:1111"


@pytest.mark.security
async def test_approved_row_under_our_own_key_never_moves_its_url(
    enabled_cluster, hello_replies
):
    """An operator-approved row is reached at the approved URL even when the
    approved key is our own — approval freezes the URL."""
    await enabled_cluster.add_peer("node-d", "https://d.gfs.test", _OWN)
    await enabled_cluster.handle_hello(
        from_node_id="node-d", url="https://other.gfs.test", public_key_hex=_OWN
    )
    row = await _node_row(enabled_cluster, "node-d")
    assert member_url(row) == "https://d.gfs.test"
    assert row.url == "https://d.gfs.test"


async def test_heartbeat_carries_our_advertised_url(enabled_cluster, monkeypatch):
    """Outbound NODE_HEARTBEAT names our cluster URL so a shared-seed
    sibling's row for us follows a redeploy within one interval."""
    sent: list[tuple[str, dict]] = []

    async def fake_post(self, url, msg_type, payload, *, to="", session=None):
        sent.append((msg_type, payload))

    async def fake_ping(self, url):
        return True

    monkeypatch.setattr(ClusterService, "_post_to_peer", fake_post)
    monkeypatch.setattr(ClusterService, "_ping_peer", fake_ping)
    await enabled_cluster.handle_hello(
        from_node_id="node-c", url="https://c.gfs.test", public_key_hex=_OWN
    )
    sent.clear()
    await enabled_cluster._heartbeat_tick()
    (beat,) = [p for t, p in sent if t == NODE_HEARTBEAT]
    assert beat["url"] == "https://a.gfs.test"


# ─── Admin removal vs. stale liveness writes ─────────────────────────
#
# A liveness refresh reads the roster, awaits I/O (a ping, a HELLO
# verify), then writes. An admin removal that lands in between must stay
# removed: a refresh never re-creates a row, and never puts back the key it
# read before the removal.


class _RemovesAfterRead:
    """Repo wrapper: the first ``list_nodes`` returns its snapshot, then
    the node is removed — an admin DELETE landing right after the read."""

    def __init__(self, repo: SqliteClusterRepo, node_id: str) -> None:
        self._repo = repo
        self._node_id = node_id
        self.armed = True

    async def list_nodes(self) -> list[ClusterNode]:
        rows = await self._repo.list_nodes()
        if self.armed:
            self.armed = False
            await self._repo.remove_node(self._node_id)
        return rows

    def __getattr__(self, name: str):
        return getattr(self._repo, name)


async def _member_verdict(svc: ClusterService, node_id: str) -> FrameVerdict:
    row = next((n for n in await svc.list_nodes() if n.node_id == node_id), None)
    return authorize_frame(
        msg_type=NODE_HEARTBEAT,
        from_node=node_id,
        carried_key="",
        row=row,
        own_key=svc.own_public_key_hex,
    )


@pytest.fixture
def held_ping(monkeypatch):
    """``_ping_peer`` blocks until the test releases it (a slow peer)."""

    class _Held:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.result = True

    held = _Held()

    async def slow_ping(self, peer_url):
        held.entered.set()
        await held.release.wait()
        return held.result

    monkeypatch.setattr(ClusterService, "_ping_peer", slow_ping)
    return held


@pytest.mark.security
@pytest.mark.parametrize("ping_ok", [True, False])
async def test_removal_during_a_held_heartbeat_ping_stays_removed(
    enabled_cluster, hello_replies, held_ping, ping_ok
):
    svc = enabled_cluster
    await svc.add_peer("node-b", "https://b.gfs.test", _PIN)
    held_ping.result = ping_ok
    # A failing ping marks the peer offline on this tick.
    svc._fail_counts["https://b.gfs.test"] = 2
    tick = asyncio.create_task(svc._heartbeat_tick())
    await held_ping.entered.wait()
    await svc.remove_peer("node-b")
    held_ping.release.set()
    await tick
    assert "node-b" not in {n.node_id for n in await svc.list_nodes()}
    assert await _member_verdict(svc, "node-b") == FrameVerdict(error="unknown_node")


@pytest.mark.security
async def test_removal_during_a_held_ping_of_an_offline_peer_stays_removed(
    enabled_cluster, hello_replies, held_ping, gfs_db
):
    svc = enabled_cluster
    await svc.add_peer("node-b", "https://b.gfs.test", _PIN)
    await gfs_db.enqueue(
        "UPDATE cluster_nodes SET status='offline' WHERE node_id='node-b'"
    )
    tick = asyncio.create_task(svc._heartbeat_tick())
    await held_ping.entered.wait()
    await svc.remove_peer("node-b")
    held_ping.release.set()
    await tick
    assert await _member_verdict(svc, "node-b") == FrameVerdict(error="unknown_node")


@pytest.mark.security
@pytest.mark.parametrize("re_add_before_release", [True, False])
async def test_a_key_rotation_is_not_reverted_by_a_held_heartbeat(
    enabled_cluster, hello_replies, held_ping, re_add_before_release
):
    """Remove, then re-add under a new key: the in-flight refresh must not
    put the old key back (nor make the re-add a key mismatch)."""
    svc = enabled_cluster
    await svc.add_peer("node-b", "https://b.gfs.test", _PIN)
    tick = asyncio.create_task(svc._heartbeat_tick())
    await held_ping.entered.wait()
    await svc.remove_peer("node-b")
    if re_add_before_release:
        await svc.add_peer("node-b", "https://b.gfs.test", _OTHER)
    held_ping.release.set()
    await tick
    if not re_add_before_release:
        await svc.add_peer("node-b", "https://b.gfs.test", _OTHER)
    assert await _member_verdict(svc, "node-b") == FrameVerdict(verify_key=_OTHER)


@pytest.mark.security
async def test_removal_racing_a_hello_stays_removed(enabled_cluster, hello_replies):
    svc = enabled_cluster
    await svc.add_peer("node-b", "https://b.gfs.test", _PIN)
    svc._repo = _RemovesAfterRead(svc._repo, "node-b")
    await svc.handle_hello(
        from_node_id="node-b", url="https://b.gfs.test", public_key_hex=_PIN
    )
    assert await _member_verdict(svc, "node-b") == FrameVerdict(error="unknown_node")


@pytest.mark.security
async def test_removal_racing_a_heartbeat_stays_removed(enabled_cluster, hello_replies):
    svc = enabled_cluster
    await svc.add_peer("node-b", "https://b.gfs.test", _PIN)
    svc._repo = _RemovesAfterRead(svc._repo, "node-b")
    await svc.handle_heartbeat("node-b", {"active_sync_sessions": 1})
    assert await _member_verdict(svc, "node-b") == FrameVerdict(error="unknown_node")


async def test_a_hello_from_an_approved_node_without_a_row_creates_nothing(
    enabled_cluster, hello_replies
):
    """Only a HELLO under our OWN key may create a row; an approved node's
    row is created by the admin alone."""
    await enabled_cluster.handle_hello(
        from_node_id="node-c", url="https://c.gfs.test", public_key_hex=_PIN
    )
    assert "node-c" not in {n.node_id for n in await enabled_cluster.list_nodes()}
    assert hello_replies == []


# ─── Replay-cache capacity: refuse, never evict ──────────────────────


@pytest.mark.security
async def test_a_full_replay_cache_never_refuses_a_fresh_honest_frame(gfs_db):
    """The review's probe: 19 200 accepted digests dated ``now + 300`` (a
    member, or many own-key node ids, saturating the cache with
    future-dated frames). Evicting them used to raise the floor to
    ``now + 300``, so every honest frame was refused as a replay for ten
    minutes. A fresh honest frame from another node must be accepted."""
    clocks = _Clocks()
    svc = _replay_svc(gfs_db, clocks)
    clocks.advance(CLUSTER_TS_SKEW_S)  # past the boot floor
    future = int(clocks.wall) + CLUSTER_TS_SKEW_S
    for i in range(19_200):
        svc.record_frame(f"attack-{i}".encode(), future, f"attacker-{i % 32}")
    now = int(clocks.wall)
    for n in range(3):
        raw = f"honest-{n}".encode()
        assert not svc.frame_seen(raw, now)
        assert svc.record_frame(raw, now, "honest")
        assert svc.frame_seen(raw, now)


async def test_announce_names_the_recipient_for_a_differently_spelled_peer_url(
    tmp_dir, gfs_db, monkeypatch
):
    """A configured peer URL spelled differently from the stored row's
    (case, trailing slash) still resolves to the row, so the HELLO is bound
    to its recipient."""
    toml = tmp_dir / "global_server.toml"
    toml.write_text(
        '[server]\nbase_url = "https://a.gfs.test"\n'
        '[cluster]\nenabled = true\npeers = ["HTTPS://B.GFS.test/"]\n'
    )
    cfg = GfsConfig.from_toml(toml)
    posted: list[tuple[str, str]] = []

    async def fake_post(self, url, msg_type, payload, *, to="", session=None):
        posted.append((url, to))

    monkeypatch.setattr(ClusterService, "_post_to_peer", fake_post)
    repo = SqliteClusterRepo(gfs_db)
    await repo.approve_node("node-b", "https://b.gfs.test", _PIN)
    svc = ClusterService(
        repo, node_id="node-a", self_url=cfg.base_url, peers=cfg.cluster_peers
    )
    await svc._announce_to_peers()
    assert posted == [("https://b.gfs.test", "node-b")]


# ─── NODE_DRAIN_HINT (cross-node envelope-queue drain) ────────────────

_HOME_A = "homea2222222222222222222222222aa"
_HOME_B = "homeb2222222222222222222222222bb"
_HOME_C = "homec2222222222222222222222222cc"


class _Connected:
    """``GfsWebSocketRegistry`` stand-in: only ``is_connected`` matters."""

    def __init__(self, online: set[str]) -> None:
        self.online = online

    def is_connected(self, instance_id: str) -> bool:
        return instance_id in self.online


def _drain_cluster(gfs_db, *, enabled=True, online=()):
    return ClusterService(
        SqliteClusterRepo(gfs_db),
        node_id="node-a",
        own_public_key_hex=_OWN,
        enabled=enabled,
        ws_registry=_Connected(set(online)),
    )


async def _flushed(svc: ClusterService) -> None:
    """Wait for the pending drain-hint flush task itself — no polling."""
    task = svc._drain_hint_task
    assert task is not None
    await asyncio.wait_for(task, timeout=2)


def _record_broadcasts(monkeypatch) -> list[tuple[str, dict, bool]]:
    sent: list[tuple[str, dict, bool]] = []

    async def fake_broadcast(
        self, msg_type, payload, *, ignore_errors=False, session=None
    ):
        sent.append((msg_type, payload, ignore_errors))

    monkeypatch.setattr(ClusterService, "_broadcast", fake_broadcast)
    return sent


async def test_hint_drain_is_a_no_op_when_the_cluster_is_disabled(gfs_db, monkeypatch):
    sent = _record_broadcasts(monkeypatch)
    svc = _drain_cluster(gfs_db, enabled=False)
    svc.hint_drain(_HOME_A)
    assert svc._drain_hint_task is None
    assert sent == []
    await svc.stop()


async def test_hint_drain_coalesces_many_ids_into_one_broadcast(gfs_db, monkeypatch):
    monkeypatch.setattr(cluster_mod, "DRAIN_HINT_DELAY_S", 0.01)
    sent = _record_broadcasts(monkeypatch)
    svc = _drain_cluster(gfs_db)
    for _ in range(20):
        svc.hint_drain(_HOME_B)
        svc.hint_drain(_HOME_A)
    await _flushed(svc)
    assert sent == [(NODE_DRAIN_HINT, {"instances": [_HOME_A, _HOME_B]}, True)]
    await svc.stop()


async def test_hint_drain_caps_ids_per_frame(gfs_db, monkeypatch):
    monkeypatch.setattr(cluster_mod, "DRAIN_HINT_DELAY_S", 0.01)
    monkeypatch.setattr(cluster_mod, "DRAIN_HINT_MAX_IDS", 2)
    sent = _record_broadcasts(monkeypatch)
    svc = _drain_cluster(gfs_db)
    for iid in (_HOME_A, _HOME_B, _HOME_C):
        svc.hint_drain(iid)
    await _flushed(svc)
    assert [p["instances"] for _t, p, _i in sent] == [[_HOME_A, _HOME_B], [_HOME_C]]
    await svc.stop()


async def test_a_hint_after_a_flush_starts_a_new_flush(gfs_db, monkeypatch):
    monkeypatch.setattr(cluster_mod, "DRAIN_HINT_DELAY_S", 0.01)
    sent = _record_broadcasts(monkeypatch)
    svc = _drain_cluster(gfs_db)
    svc.hint_drain(_HOME_A)
    await _flushed(svc)
    svc.hint_drain(_HOME_B)
    await _flushed(svc)
    assert [p["instances"] for _t, p, _i in sent] == [[_HOME_A], [_HOME_B]]
    await svc.stop()


async def test_a_failing_flush_is_logged_and_the_next_hint_still_flushes(
    gfs_db, monkeypatch, caplog
):
    monkeypatch.setattr(cluster_mod, "DRAIN_HINT_DELAY_S", 0.01)
    calls: list[dict] = []

    async def flaky(self, msg_type, payload, *, ignore_errors=False, session=None):
        calls.append(payload)
        if len(calls) == 1:
            raise RuntimeError("roster read failed")

    monkeypatch.setattr(ClusterService, "_broadcast", flaky)
    svc = _drain_cluster(gfs_db)
    with caplog.at_level("WARNING"):
        svc.hint_drain(_HOME_A)
        await _flushed(svc)
    assert "drain hint" in caplog.text
    svc.hint_drain(_HOME_B)
    await _flushed(svc)
    assert calls[-1] == {"instances": [_HOME_B]}
    await svc.stop()


async def test_stop_abandons_a_pending_hint(gfs_db, monkeypatch):
    sent = _record_broadcasts(monkeypatch)
    svc = _drain_cluster(gfs_db)
    svc.hint_drain(_HOME_A)
    task = svc._drain_hint_task
    await svc.stop()
    assert task is not None and task.done()
    assert sent == []


async def test_apply_drain_hint_drains_only_locally_connected_valid_ids(gfs_db):
    svc = _drain_cluster(gfs_db, online={_HOME_A, "not-an-instance-id"})
    drained: list[str] = []

    async def _drain(instance_id: str) -> int:
        drained.append(instance_id)
        return 1

    svc.attach_drain(_drain)
    scheduled = await svc.apply_drain_hint(
        [
            _HOME_A,
            _HOME_A,  # duplicate → one drain
            _HOME_B,  # registered elsewhere, not on this node
            "not-an-instance-id",  # connected, but not an instance id
            _HOME_A.upper(),
            _HOME_A + "\nforged",
            42,
            None,
        ]
    )
    assert scheduled == 1
    await svc.stop()
    assert drained == [_HOME_A]


@pytest.mark.parametrize("instances", [None, "x", {"a": 1}, 7])
async def test_apply_drain_hint_ignores_a_malformed_payload(gfs_db, instances):
    svc = _drain_cluster(gfs_db, online={_HOME_A})
    drained: list[str] = []

    async def _drain(instance_id: str) -> int:
        drained.append(instance_id)
        return 0

    svc.attach_drain(_drain)
    assert await svc.apply_drain_hint(instances) == 0
    await svc.stop()
    assert drained == []


async def test_apply_drain_hint_without_a_drain_callback_is_a_no_op(gfs_db):
    svc = _drain_cluster(gfs_db, online={_HOME_A})
    assert await svc.apply_drain_hint([_HOME_A]) == 0


async def test_apply_drain_hint_caps_the_ids_it_reads(gfs_db, monkeypatch):
    monkeypatch.setattr(cluster_mod, "DRAIN_HINT_MAX_IDS", 1)
    svc = _drain_cluster(gfs_db, online={_HOME_A, _HOME_B})
    drained: list[str] = []

    async def _drain(instance_id: str) -> int:
        drained.append(instance_id)
        return 0

    svc.attach_drain(_drain)
    assert await svc.apply_drain_hint([_HOME_A, _HOME_B]) == 1
    await svc.stop()
    assert drained == [_HOME_A]


async def test_a_failing_hint_drain_is_logged(gfs_db, caplog):
    svc = _drain_cluster(gfs_db, online={_HOME_A})

    async def _drain(instance_id: str) -> int:
        raise RuntimeError("socket gone")

    svc.attach_drain(_drain)
    with caplog.at_level("WARNING"):
        await svc.apply_drain_hint([_HOME_A])
        await svc.stop()
    assert "hint drain failed" in caplog.text


async def test_a_repeated_hint_does_not_stack_drains_for_one_household(gfs_db):
    """Hints for a household whose hinted drain is still running start no
    second task: however many arrive, they coalesce into ONE more drain
    after the running one (which may have listed before the new rows)."""
    svc = _drain_cluster(gfs_db, online={_HOME_A})
    release = asyncio.Event()
    started = asyncio.Event()
    calls: list[str] = []

    async def _drain(instance_id: str) -> int:
        calls.append(instance_id)
        started.set()
        await release.wait()
        return 0

    svc.attach_drain(_drain)
    assert await svc.apply_drain_hint([_HOME_A]) == 1
    await asyncio.wait_for(started.wait(), timeout=2)
    assert await svc.apply_drain_hint([_HOME_A]) == 0
    assert await svc.apply_drain_hint([_HOME_A]) == 0
    release.set()
    await asyncio.wait_for(svc._hint_drains[_HOME_A], timeout=2)
    assert calls == [_HOME_A, _HOME_A]
    assert svc._hint_drains == {}
    await svc.stop()


async def test_a_hint_after_the_drain_finished_drains_again(gfs_db):
    svc = _drain_cluster(gfs_db, online={_HOME_A})
    calls: list[str] = []

    async def _drain(instance_id: str) -> int:
        calls.append(instance_id)
        return 0

    svc.attach_drain(_drain)
    assert await svc.apply_drain_hint([_HOME_A]) == 1
    # Awaiting the task resumes us after its own bookkeeping callback ran.
    await asyncio.wait_for(svc._hint_drains[_HOME_A], timeout=2)
    assert await svc.apply_drain_hint([_HOME_A]) == 1
    await svc.stop()
    assert calls == [_HOME_A, _HOME_A]
