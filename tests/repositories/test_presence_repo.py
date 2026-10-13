"""Tests for :class:`SqlitePresenceRepo`."""

from __future__ import annotations

import pytest

from socialhome.repositories.presence_repo import SqlitePresenceRepo


@pytest.fixture
async def repo(db):
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, picture_hash)"
        " VALUES(?,?,?,?)",
        ("anna", "uid-anna", "Anna", "abc123"),
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("ben", "uid-ben", "Ben"),
    )
    return SqlitePresenceRepo(db)


async def _upsert(repo, username: str, updated_at: str) -> None:
    await repo.upsert_local(
        username=username,
        entity_id=None,
        state="home",
        zone_name="home",
        latitude=47.1234,
        longitude=8.5678,
        gps_accuracy_m=10.0,
        updated_at=updated_at,
    )


async def test_list_active_carries_updated_at_and_picture(repo):
    await _upsert(repo, "anna", "2026-10-13T08:00:00+00:00")
    await _upsert(repo, "ben", "2026-10-13T09:00:00+00:00")
    rows = {p.username: p for p in await repo.list_active()}
    assert rows["anna"].updated_at == "2026-10-13T08:00:00+00:00"
    assert rows["anna"].picture_url == "api/users/uid-anna/picture?v=abc123"
    assert rows["ben"].updated_at == "2026-10-13T09:00:00+00:00"
    assert rows["ben"].picture_url is None


async def test_get_by_username_carries_updated_at(repo):
    await _upsert(repo, "anna", "2026-10-13T08:00:00+00:00")
    row = await repo.get_by_username("anna")
    assert row is not None
    assert row.updated_at == "2026-10-13T08:00:00+00:00"
    assert await repo.get_by_username("nobody") is None
