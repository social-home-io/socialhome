"""Tests for SpaceRetentionScheduler (§27181, §47092)."""

from __future__ import annotations

import asyncio

import pytest

from social_home.db.database import AsyncDatabase
from social_home.infrastructure.space_retention_scheduler import (
    SpaceRetentionScheduler,
)


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    # Seed a space with 7-day retention.
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, "
        "identity_public_key, retention_days, retention_exempt_json) "
        "VALUES('sp-1', 't', 'iid', 'u', ?, 7, '[]')",
        ("aa" * 32,),
    )
    yield db
    await db.shutdown()


async def _seed_post(db, post_id, *, days_old, type_="text"):
    from datetime import datetime, timedelta, timezone

    created = (datetime.now(timezone.utc) - timedelta(days=days_old)).strftime(
        "%Y-%m-%d %H:%M:%S",
    )
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content, created_at) "
        "VALUES(?, 'sp-1', 'u-author', ?, 'hi', ?)",
        (post_id, type_, created),
    )


async def test_prune_deletes_old_posts(env):
    """Posts older than retention_days get soft-deleted."""
    db = env
    await _seed_post(db, "old", days_old=10)
    await _seed_post(db, "fresh", days_old=1)
    sched = SpaceRetentionScheduler(db)
    n = await sched._prune_once()
    assert n == 1
    rows = {
        r["id"]: r["deleted"]
        for r in await db.fetchall(
            "SELECT id, deleted FROM space_posts",
        )
    }
    assert rows == {"old": 1, "fresh": 0}


async def test_exempt_types_are_kept(env):
    """Posts whose type is in retention_exempt_json survive."""
    db = env
    await db.enqueue(
        "UPDATE spaces SET retention_exempt_json='[\"poll\"]' WHERE id='sp-1'",
    )
    await _seed_post(db, "old-poll", days_old=10, type_="poll")
    await _seed_post(db, "old-text", days_old=10, type_="text")
    sched = SpaceRetentionScheduler(db)
    n = await sched._prune_once()
    assert n == 1
    rows = {
        r["id"]: r["deleted"]
        for r in await db.fetchall(
            "SELECT id, deleted FROM space_posts",
        )
    }
    assert rows == {"old-poll": 0, "old-text": 1}


async def test_no_retention_means_no_prune(env):
    """Spaces with retention_days IS NULL are skipped."""
    db = env
    await db.enqueue("UPDATE spaces SET retention_days=NULL WHERE id='sp-1'")
    await _seed_post(db, "old", days_old=10)
    sched = SpaceRetentionScheduler(db)
    n = await sched._prune_once()
    assert n == 0


async def test_scheduler_lifecycle_safe(env):
    db = env
    s = SpaceRetentionScheduler(db, interval_seconds=10.0)
    await s.start()
    await s.start()  # idempotent
    await s.stop()


async def test_scheduler_loop_ticks(env):
    db = env
    s = SpaceRetentionScheduler(db, interval_seconds=0.05)
    await s.start()
    await asyncio.sleep(0.12)
    await s.stop()
