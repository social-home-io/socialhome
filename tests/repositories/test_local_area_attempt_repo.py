"""``SqliteLocalAreaAttemptRepo`` — the daily local-community budgets."""

from __future__ import annotations

import asyncio

import pytest

from socialhome.db import AsyncDatabase
from socialhome.repositories.local_area_attempt_repo import (
    SqliteLocalAreaAttemptRepo,
)

DAY = "2026-10-19"
KEEP = "2026-10-18"


@pytest.fixture
async def db(tmp_path):
    db = AsyncDatabase(str(tmp_path / "test.db"), batch_timeout_ms=10)
    await db.startup()
    yield db
    await db.shutdown()


async def _counts(db) -> dict[tuple[str, str, str, str], int]:
    rows = await db.fetchall(
        "SELECT space_id, subject_kind, subject_id, day, count FROM local_area_attempts"
    )
    return {(r[0], r[1], r[2], r[3]): r[4] for r in rows}


async def test_spends_up_to_the_limit_then_refuses(db):
    repo = SqliteLocalAreaAttemptRepo(db)
    lines = [("person", "p1", 3)]
    assert [
        await repo.try_spend("s1", DAY, lines, prune_before=KEEP) for _ in range(5)
    ] == [
        True,
        True,
        True,
        False,
        False,
    ]
    assert await _counts(db) == {("s1", "person", "p1", DAY): 3}


async def test_all_or_nothing_across_lines(db):
    """A line at its limit refuses the whole spend: the other line is not
    touched (a held-off person never drains the household's backstop)."""
    repo = SqliteLocalAreaAttemptRepo(db)
    person = ("person", "p1", 1)
    household = ("household", "h1", 10)
    assert await repo.try_spend("s1", DAY, [person, household], prune_before=KEEP)
    assert not await repo.try_spend("s1", DAY, [person, household], prune_before=KEEP)
    assert await _counts(db) == {
        ("s1", "person", "p1", DAY): 1,
        ("s1", "household", "h1", DAY): 1,
    }


async def test_space_subject_and_day_are_separate_counters(db):
    repo = SqliteLocalAreaAttemptRepo(db)
    line = [("person", "p1", 1)]
    assert await repo.try_spend("s1", DAY, line, prune_before=KEEP)
    assert await repo.try_spend("s2", DAY, line, prune_before=KEEP)
    assert await repo.try_spend("s1", DAY, [("person", "p2", 1)], prune_before=KEEP)
    assert await repo.try_spend("s1", "2026-10-20", line, prune_before=DAY)


async def test_old_days_are_pruned_on_write(db):
    repo = SqliteLocalAreaAttemptRepo(db)
    line = [("person", "p1", 3)]
    await repo.try_spend("s1", "2026-10-17", line, prune_before="2026-10-16")
    await repo.try_spend("s1", KEEP, line, prune_before="2026-10-17")
    await repo.try_spend("s1", DAY, line, prune_before=KEEP)
    assert set(await _counts(db)) == {
        ("s1", "person", "p1", KEEP),
        ("s1", "person", "p1", DAY),
    }


async def test_concurrent_spends_never_overshoot(db):
    repo = SqliteLocalAreaAttemptRepo(db)
    line = [("person", "p1", 3)]
    results = await asyncio.gather(
        *(repo.try_spend("s1", DAY, line, prune_before=KEEP) for _ in range(10))
    )
    assert results.count(True) == 3
    assert await _counts(db) == {("s1", "person", "p1", DAY): 3}


async def test_the_count_survives_a_restart(tmp_path):
    path = str(tmp_path / "restart.db")
    db = AsyncDatabase(path, batch_timeout_ms=10)
    await db.startup()
    try:
        repo = SqliteLocalAreaAttemptRepo(db)
        for _ in range(3):
            assert await repo.try_spend(
                "s1", DAY, [("person", "p1", 3)], prune_before=KEEP
            )
    finally:
        await db.shutdown()
    db = AsyncDatabase(path, batch_timeout_ms=10)
    await db.startup()
    try:
        repo = SqliteLocalAreaAttemptRepo(db)
        assert not await repo.try_spend(
            "s1", DAY, [("person", "p1", 3)], prune_before=KEEP
        )
    finally:
        await db.shutdown()
