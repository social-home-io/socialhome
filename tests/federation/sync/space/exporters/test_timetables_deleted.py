"""Tests for socialhome.federation.sync.space.exporters.timetables_deleted."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from socialhome.domain.timetable import Timetable
from socialhome.federation.owner_bound_id import (
    SPACE_TIMETABLE_KIND,
    mint_owner_bound_id,
)
from socialhome.federation.sync.space.exporter import (
    REMOVAL_RESOURCES,
    RESOURCE_ORDER,
)
from socialhome.federation.sync.space.exporters import TimetablesDeletedExporter
from socialhome.federation.sync.space.window import SYNC_PAGE_SIZE
from socialhome.repositories.timetable_repo import SqliteSpaceTimetableRepo

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
async def repo(db):
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp','S','h','anna','ab')"
    )
    return SqliteSpaceTimetableRepo(db)


def _tt(name: str) -> Timetable:
    return Timetable(
        id=mint_owner_bound_id(
            SPACE_TIMETABLE_KIND, space_id="sp", owner_user_id="u-a"
        ),
        name=name,
        created_by="u-a",
        created_at=T0,
        updated_at=T0,
    )


async def test_streams_every_tombstone_without_content(repo):
    gone, kept = _tt("Secret school"), _tt("Still here")
    for tt in (gone, kept):
        await repo.insert(tt, space_id="sp")
    await repo.soft_delete(gone.id, space_id="sp", at=T0 + timedelta(days=1))
    records = await TimetablesDeletedExporter(repo).list_records("sp")
    assert [r["id"] for r in records] == [gone.id]
    assert set(records[0]) == {"id", "created_by", "created_at"}
    assert records[0]["created_by"] == "u-a"
    assert "Secret" not in repr(records) and kept.id not in repr(records)


async def test_pages_past_one_page_with_no_window(repo):
    for i in range(SYNC_PAGE_SIZE + 3):
        await repo.tombstone(f"tt-{i}", space_id="sp", at=T0)
    exporter = TimetablesDeletedExporter(repo)
    batches = [b async for b in exporter.iter_batches("sp")]
    assert [len(b) for b in batches] == [SYNC_PAGE_SIZE, 3]


async def test_streams_only_tombstones_changed_since(repo):
    first, second = _tt("A"), _tt("B")
    for tt in (first, second):
        await repo.insert(tt, space_id="sp")
    await repo.soft_delete(first.id, space_id="sp", at=T0)
    mark = int(await repo._db.fetchval("SELECT seq FROM sync_seq_counter", (), 0))
    await repo.soft_delete(second.id, space_id="sp", at=T0)
    exporter = TimetablesDeletedExporter(repo)
    changed = [r async for b in exporter.iter_changed("sp", mark) for r in b]
    assert [r["id"] for r in changed] == [second.id]


def test_ships_before_the_live_timetables_as_a_removal():
    assert RESOURCE_ORDER.index("timetables_deleted") < RESOURCE_ORDER.index(
        "timetables"
    )
    assert "timetables_deleted" in REMOVAL_RESOURCES
