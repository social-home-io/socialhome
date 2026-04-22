"""Tests for ReplayCachePruneScheduler (§24.11)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.repositories.federation_repo import SqliteFederationRepo
from socialhome.infrastructure.replay_cache_scheduler import (
    ReplayCachePruneScheduler,
)


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    repo = SqliteFederationRepo(db)
    yield db, repo
    await db.shutdown()


async def test_prune_once_deletes_old_rows(env):
    """Rows with received_at older than the window are dropped."""
    db, repo = env
    # Seed one fresh + one stale row directly.
    await db.enqueue(
        "INSERT INTO federation_replay_cache(msg_id, received_at) "
        "VALUES('fresh', datetime('now'))",
    )
    await db.enqueue(
        "INSERT INTO federation_replay_cache(msg_id, received_at) "
        "VALUES('stale', '2020-01-01 00:00:00')",
    )
    sched = ReplayCachePruneScheduler(repo, window=timedelta(hours=1))
    n = await sched._prune_once()
    assert n == 1
    rows = await db.fetchall(
        "SELECT msg_id FROM federation_replay_cache",
    )
    msg_ids = {r["msg_id"] for r in rows}
    assert msg_ids == {"fresh"}


async def test_double_start_is_idempotent(env):
    _, repo = env
    sched = ReplayCachePruneScheduler(repo, interval_seconds=10.0)
    await sched.start()
    await sched.start()  # no-op
    await sched.stop()


async def test_stop_without_start_is_safe(env):
    _, repo = env
    sched = ReplayCachePruneScheduler(repo)
    await sched.stop()


async def test_loop_runs_periodically(env):
    """Quick interval lets the loop tick at least once."""
    _, repo = env
    sched = ReplayCachePruneScheduler(repo, interval_seconds=0.05)
    await sched.start()
    await asyncio.sleep(0.12)
    await sched.stop()
