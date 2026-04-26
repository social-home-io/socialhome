"""Tests for ClusterService — single-node GFS cluster stub."""

from __future__ import annotations

import pytest

from socialhome.global_server.cluster import (
    MAX_SIGNALING_SESSIONS,
    ClusterService,
)
from socialhome.global_server.domain import ClusterNode
from socialhome.global_server.repositories import SqliteClusterRepo


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
        enabled=True,
    )
    # Self row exists so update_active_sync_sessions can target it.
    await repo.upsert_node(
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


async def test_get_leader_none_when_no_nodes(cluster):
    """get_leader() returns None when no nodes have been registered."""
    leader = await cluster.get_leader()
    assert leader is None


async def test_announce_single_node(cluster):
    """announce() registers a node that appears in list_nodes()."""
    await cluster.announce("node-1", "https://gfs1.example.com")
    nodes = await cluster.list_nodes()
    assert len(nodes) == 1
    assert nodes[0].node_id == "node-1"
    assert nodes[0].address == "https://gfs1.example.com"


async def test_announce_multiple_nodes(cluster):
    """Multiple nodes are all returned by list_nodes()."""
    await cluster.announce("node-a", "https://gfs-a.example.com")
    await cluster.announce("node-b", "https://gfs-b.example.com")
    nodes = await cluster.list_nodes()
    node_ids = {n.node_id for n in nodes}
    assert "node-a" in node_ids
    assert "node-b" in node_ids


async def test_get_leader_returns_first_registered(cluster):
    """get_leader() returns the first-registered node_id."""
    await cluster.announce("node-first", "https://first.example.com")
    await cluster.announce("node-second", "https://second.example.com")
    leader = await cluster.get_leader()
    assert leader == "node-first"


async def test_announce_is_idempotent(cluster):
    """Announcing the same node_id twice updates the address without duplicating."""
    await cluster.announce("node-dup", "https://old.example.com")
    await cluster.announce("node-dup", "https://new.example.com")
    nodes = await cluster.list_nodes()
    matching = [n for n in nodes if n.node_id == "node-dup"]
    assert len(matching) == 1
    assert matching[0].address == "https://new.example.com"


async def test_single_node_is_its_own_leader(cluster):
    """With exactly one node, that node is the leader."""
    await cluster.announce("only-node", "https://solo.example.com")
    leader = await cluster.get_leader()
    assert leader == "only-node"


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
    await repo.upsert_node(
        ClusterNode(
            node_id="node-b",
            url="https://b.gfs.test",
            status="online",
        )
    )
    await repo.upsert_node(
        ClusterNode(
            node_id="node-c",
            url="https://c.gfs.test",
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
    await repo.upsert_node(
        ClusterNode(node_id="node-z", url="https://z.gfs.test", status="online"),
    )
    await repo.upsert_node(
        ClusterNode(node_id="node-m", url="https://m.gfs.test", status="online"),
    )
    chosen = await enabled_cluster.pick_signaling_node()
    # All three (a, m, z) have count 0 → 'node-a' wins by node_id sort.
    assert chosen == "https://a.gfs.test"


async def test_pick_signaling_node_skips_offline_peers(enabled_cluster, gfs_db):
    """Offline peers are excluded from the candidate set even at zero load."""
    repo = SqliteClusterRepo(gfs_db)
    await repo.upsert_node(
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
    await repo.upsert_node(
        ClusterNode(node_id="node-b", url="https://b.gfs.test", status="online"),
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
    await repo.upsert_node(
        ClusterNode(node_id="node-b", url="https://b.gfs.test", status="online"),
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
    await repo.upsert_node(
        ClusterNode(node_id="node-b", url="https://b.gfs.test", status="online"),
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
    await repo.upsert_node(
        ClusterNode(node_id="node-b", url="https://b.gfs.test", status="online"),
    )
    await enabled_cluster.handle_heartbeat("node-b", None)
    nodes = await repo.list_nodes()
    peer_row = next(n for n in nodes if n.node_id == "node-b")
    assert peer_row.status == "online"
    assert "node-b" not in enabled_cluster._active_sync_count
