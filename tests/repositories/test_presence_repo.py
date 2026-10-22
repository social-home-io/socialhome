"""Tests for :class:`SqlitePresenceRepo`."""

from __future__ import annotations

import pytest

from socialhome.repositories.presence_repo import SqlitePresenceRepo


@pytest.fixture
async def repo(db):
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, picture_hash)"
        " VALUES(?,?,?,?)",
        ("anna", "uid-anna", "Anna", "abc123abc123abc1"),
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
    assert rows["anna"].picture_url == "api/users/uid-anna/picture?v=abc123abc123abc1"
    assert rows["ben"].updated_at == "2026-10-13T09:00:00+00:00"
    assert rows["ben"].picture_url is None


async def test_get_by_username_carries_updated_at(repo):
    await _upsert(repo, "anna", "2026-10-13T08:00:00+00:00")
    row = await repo.get_by_username("anna")
    assert row is not None
    assert row.updated_at == "2026-10-13T08:00:00+00:00"
    assert await repo.get_by_username("nobody") is None


async def _fix(repo, at: str, *, lat=47.1234, state="away", zone=None) -> None:
    await repo.upsert_local(
        username="anna",
        entity_id=None,
        state=state,
        zone_name=zone,
        latitude=lat,
        longitude=8.5678,
        gps_accuracy_m=10.0,
        updated_at=at,
    )


async def test_located_since_stays_while_the_place_stays(repo):
    """``updated_at`` is the last fix (the dashboard map fades on it);
    ``located_since`` is when the place last changed ("here since")."""
    await _fix(repo, "2026-10-13T08:00:00+00:00")
    await _fix(repo, "2026-10-13T08:05:00+00:00")
    row = await repo.get_by_username("anna")
    assert row.updated_at == "2026-10-13T08:05:00+00:00"
    assert row.located_since == "2026-10-13T08:00:00+00:00"


async def test_located_since_moves_with_the_point_state_or_zone(repo):
    await _fix(repo, "2026-10-13T08:00:00+00:00")
    await _fix(repo, "2026-10-13T09:00:00+00:00", lat=47.1235)
    assert (await repo.get_by_username("anna")).located_since == (
        "2026-10-13T09:00:00+00:00"
    )
    await _fix(repo, "2026-10-13T10:00:00+00:00", lat=47.1235, state="zone", zone="Gym")
    assert (await repo.get_by_username("anna")).located_since == (
        "2026-10-13T10:00:00+00:00"
    )


async def test_a_row_from_before_0121_starts_its_since_at_the_next_fix(repo, db):
    await _fix(repo, "2026-10-13T08:00:00+00:00")
    await db.enqueue("UPDATE presence SET located_since=NULL WHERE username='anna'")
    await _fix(repo, "2026-10-13T08:05:00+00:00")
    assert (await repo.get_by_username("anna")).located_since == (
        "2026-10-13T08:05:00+00:00"
    )
