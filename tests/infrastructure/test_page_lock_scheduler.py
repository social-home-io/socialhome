"""Tests for PageLockExpiryScheduler."""

from __future__ import annotations

import asyncio

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.infrastructure.page_lock_scheduler import (
    PageLockExpiryScheduler,
)
from socialhome.repositories.page_repo import SqlitePageRepo


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    repo = SqlitePageRepo(db)
    yield db, repo
    await db.shutdown()


async def test_start_stop_idempotent(env):
    _, repo = env
    s = PageLockExpiryScheduler(repo, interval_seconds=10.0)
    await s.start()
    await s.start()  # no-op
    await s.stop()


async def test_stop_without_start_safe(env):
    _, repo = env
    s = PageLockExpiryScheduler(repo)
    await s.stop()


async def test_loop_calls_release(env):
    """Quick interval lets the loop tick at least once."""
    _, repo = env
    s = PageLockExpiryScheduler(repo, interval_seconds=0.05)
    await s.start()
    await asyncio.sleep(0.12)
    await s.stop()


async def test_release_expired_locks_actually_clears(env):
    """Seed a lock that's already past its TTL and confirm one prune pass clears it."""
    db, repo = env
    # Seed a page with an expired lock directly.
    await db.enqueue(
        """
        INSERT INTO pages(id, title, content, created_by,
                          locked_by, locked_at, lock_expires_at)
        VALUES('p-1', 't', 'c', 'u',
               'u', '2020-01-01T00:00:00+00:00',
               '2020-01-01T00:30:00+00:00')
        """,
    )
    n = await repo.release_expired_locks()
    assert n >= 1
    row = await db.fetchone(
        "SELECT locked_by FROM pages WHERE id='p-1'",
    )
    assert row["locked_by"] is None
