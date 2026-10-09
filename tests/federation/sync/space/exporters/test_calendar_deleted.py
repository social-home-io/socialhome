"""Tests for socialhome.federation.sync.space.exporters.calendar_deleted."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.domain.calendar import CalendarEvent
from socialhome.federation.sync.space.exporter import (
    REMOVAL_RESOURCES,
    RESOURCE_ORDER,
)
from socialhome.federation.sync.space.exporters import CalendarDeletedExporter
from socialhome.federation.sync.space.window import SYNC_PAGE_SIZE
from socialhome.repositories.calendar_repo import SqliteSpaceCalendarRepo


@pytest.fixture
async def repo(db):
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp','S','h','anna','ab')"
    )
    return SqliteSpaceCalendarRepo(db)


def _ev(eid: str) -> CalendarEvent:
    at = datetime(2020, 1, 1, tzinfo=timezone.utc)  # long past: no window
    return CalendarEvent(
        id=eid,
        calendar_id="sp",
        summary="Secret party",
        start=at,
        end=at,
        created_by="u-a",
        location="Somewhere private",
    )


async def test_streams_every_tombstone_without_content(repo):
    await repo.save_event(_ev("ev-gone"), space_id="sp")
    await repo.save_event(_ev("ev-kept"), space_id="sp")
    await repo.delete_event("ev-gone", space_id="sp", deleted_by="u-b")
    records = await CalendarDeletedExporter(repo).list_records("sp")
    assert [r["id"] for r in records] == ["ev-gone"]
    assert set(records[0]) == {"id", "created_by", "created_at", "actor_user_id"}
    assert records[0]["created_by"] == "u-a"
    assert "Secret" not in repr(records) and "private" not in repr(records)


async def test_pages_past_one_page_with_no_window(repo):
    for i in range(SYNC_PAGE_SIZE + 1):
        await repo.tombstone_event(f"ev-{i}", space_id="sp", created_by="u-a")
    batches = [b async for b in CalendarDeletedExporter(repo).iter_batches("sp")]
    assert [len(b) for b in batches] == [SYNC_PAGE_SIZE, 1]


def test_ships_before_the_live_calendar_as_a_removal():
    assert RESOURCE_ORDER.index("calendar_deleted") < RESOURCE_ORDER.index("calendar")
    assert "calendar_deleted" in REMOVAL_RESOURCES
