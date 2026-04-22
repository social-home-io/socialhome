"""Tests for ClusterService — single-node GFS cluster stub."""

from __future__ import annotations

import pytest

from socialhome.global_server.cluster import ClusterService
from socialhome.global_server.repositories import SqliteClusterRepo


@pytest.fixture
async def cluster(gfs_db):
    """A ClusterService backed by the shared GFS database fixture."""
    repo = SqliteClusterRepo(gfs_db)
    return ClusterService(repo)


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
