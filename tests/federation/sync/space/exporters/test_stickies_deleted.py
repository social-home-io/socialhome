"""Tests for socialhome.federation.sync.space.exporters.stickies_deleted."""

from __future__ import annotations

import pytest

from socialhome.federation.sync.space.exporter import (
    REMOVAL_RESOURCES,
    RESOURCE_ORDER,
)
from socialhome.federation.sync.space.exporters import StickiesDeletedExporter
from socialhome.federation.sync.space.window import SYNC_PAGE_SIZE
from socialhome.repositories.sticky_repo import SqliteStickyRepo


@pytest.fixture
async def repo(db):
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp','S','h','anna','ab')"
    )
    return SqliteStickyRepo(db)


async def test_streams_every_tombstone_without_content(repo):
    gone = await repo.add(author="u-a", content="secret words", space_id="sp")
    kept = await repo.add(author="u-a", content="still here", space_id="sp")
    await repo.delete(gone.id, space_id="sp", deleted_by="u-b")
    records = await StickiesDeletedExporter(repo).list_records("sp")
    assert [r["id"] for r in records] == [gone.id]
    assert set(records[0]) == {"id", "author", "created_at", "actor_user_id"}
    assert records[0]["author"] == "u-a" and records[0]["actor_user_id"] == "u-b"
    assert "secret" not in repr(records) and kept.id not in repr(records)


async def test_pages_past_one_page_with_no_window(repo):
    for i in range(SYNC_PAGE_SIZE + 3):
        await repo.tombstone(f"st-{i}", space_id="sp", author="u-a")
    exporter = StickiesDeletedExporter(repo)
    batches = [b async for b in exporter.iter_batches("sp")]
    assert [len(b) for b in batches] == [SYNC_PAGE_SIZE, 3]


def test_ships_before_the_live_stickies_as_a_removal():
    assert RESOURCE_ORDER.index("stickies_deleted") < RESOURCE_ORDER.index("stickies")
    assert "stickies_deleted" in REMOVAL_RESOURCES
