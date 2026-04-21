"""Tests for PostDraftCleanupScheduler (§30631)."""

from __future__ import annotations

import asyncio

import pytest

from social_home.db.database import AsyncDatabase
from social_home.infrastructure.post_draft_scheduler import (
    PostDraftCleanupScheduler,
)


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES('u', 'u-uid', 'U')",
    )
    yield db
    await db.shutdown()


async def test_prune_old_drafts(env):
    db = env
    await db.enqueue(
        "INSERT INTO post_drafts(id, username, context, content, updated_at) "
        "VALUES('d-old', 'u', 'household_feed', 'old', '2020-01-01 00:00:00')",
    )
    await db.enqueue(
        "INSERT INTO post_drafts(id, username, context, content, updated_at) "
        "VALUES('d-fresh', 'u', 'household_feed', 'fresh', datetime('now'))",
    )
    sched = PostDraftCleanupScheduler(db, ttl_days=30)
    n = await sched._prune_once()
    assert n == 1
    rows = {r["id"] for r in await db.fetchall("SELECT id FROM post_drafts")}
    assert rows == {"d-fresh"}


async def test_no_stale_drafts_returns_zero(env):
    db = env
    sched = PostDraftCleanupScheduler(db)
    assert await sched._prune_once() == 0


async def test_lifecycle_idempotent(env):
    s = PostDraftCleanupScheduler(env, interval_seconds=10.0)
    await s.start()
    await s.start()  # no-op
    await s.stop()


async def test_stop_without_start_safe(env):
    s = PostDraftCleanupScheduler(env)
    await s.stop()


async def test_loop_ticks(env):
    s = PostDraftCleanupScheduler(env, interval_seconds=0.05)
    await s.start()
    await asyncio.sleep(0.12)
    await s.stop()
