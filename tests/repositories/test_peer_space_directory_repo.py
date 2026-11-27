"""Tests for socialhome.repositories.peer_space_directory_repo."""

from __future__ import annotations

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.repositories.peer_space_directory_repo import (
    PeerSpaceDirectoryEntry,
    SqlitePeerSpaceDirectoryRepo,
)


@pytest.fixture
async def repo(tmp_dir):
    db = AsyncDatabase(tmp_dir / "psd.db", batch_timeout_ms=10)
    await db.startup()
    yield SqlitePeerSpaceDirectoryRepo(db)
    await db.shutdown()


async def test_replace_snapshot_swaps_one_peers_rows(repo):
    await repo.replace_snapshot(
        "peer-a",
        [
            PeerSpaceDirectoryEntry(instance_id="peer-a", space_id="s1", name="One"),
            PeerSpaceDirectoryEntry(instance_id="peer-a", space_id="s2", name="Two"),
        ],
    )
    await repo.replace_snapshot(
        "peer-a",
        [PeerSpaceDirectoryEntry(instance_id="peer-a", space_id="s2", name="Two")],
    )
    assert [e.space_id for e in await repo.list_for_instance("peer-a")] == ["s2"]


async def test_get_names_the_peer_that_lists_a_space(repo):
    """Where a follow of a space only a paired household's directory lists
    finds its host (v_96)."""
    await repo.replace_snapshot(
        "peer-a",
        [PeerSpaceDirectoryEntry(instance_id="peer-a", space_id="s1", name="One")],
    )
    entry = await repo.get_any("s1")
    assert entry is not None
    assert entry.instance_id == "peer-a" and entry.name == "One"
    assert await repo.get_any("s-none") is None


async def test_clear_instance_drops_its_rows(repo):
    await repo.replace_snapshot(
        "peer-a",
        [PeerSpaceDirectoryEntry(instance_id="peer-a", space_id="s1", name="One")],
    )
    await repo.clear_instance("peer-a")
    assert await repo.get_any("s1") is None
    assert await repo.list_all() == []
