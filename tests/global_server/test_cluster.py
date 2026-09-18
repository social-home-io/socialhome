"""Tests for ClusterService — single-node GFS cluster stub."""

from __future__ import annotations

import pytest

from socialhome.crypto import generate_identity_keypair
from socialhome.capabilities_sig import (
    CAPS_SIG_SUITE_ED25519,
    verify_capabilities,
)
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


async def test_announce_is_idempotent(cluster):
    """Announcing the same node_id twice updates the address without duplicating."""
    await cluster.announce("node-dup", "https://old.example.com")
    await cluster.announce("node-dup", "https://new.example.com")
    nodes = await cluster.list_nodes()
    matching = [n for n in nodes if n.node_id == "node-dup"]
    assert len(matching) == 1
    assert matching[0].address == "https://new.example.com"


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
    await repo.upsert_node(
        ClusterNode(node_id="node-b", url="https://b.gfs.test", status="online"),
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
    await repo.upsert_node(
        ClusterNode(node_id="node-b", url="https://b.gfs.test", status="online"),
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
    await repo.upsert_node(
        ClusterNode(node_id="node-b", url="https://b.gfs.test", status="online"),
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
    await repo.upsert_node(
        ClusterNode(node_id="node-a", url="https://a.gfs.test", status="online"),
    )
    await repo.upsert_node(
        ClusterNode(node_id="node-b", url="https://b.gfs.test", status="online"),
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
