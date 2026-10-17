"""Tests for the password-reset cleanup scheduler."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.db import AsyncDatabase
from socialhome.infrastructure.password_reset_cleanup_scheduler import (
    PasswordResetCleanupScheduler,
)
from socialhome.repositories.password_reset_repo import (
    SqlitePasswordResetRepo,
)


@pytest.fixture
async def db(tmp_path):
    sqlite = tmp_path / "test.db"
    db = AsyncDatabase(str(sqlite))
    await db.startup()
    await db.enqueue(
        "INSERT INTO platform_users(username, display_name, is_admin, "
        "password_hash) VALUES('alice','Alice',0,'x')",
        (),
    )
    yield db
    await db.shutdown()


async def test_prune_once_drops_expired_rows(db):
    repo = SqlitePasswordResetRepo(db)
    past = datetime.now(UTC) - timedelta(hours=2)
    await repo.create_token("alice", "admin", ttl_seconds=3600, now=past)
    await repo.create_token("alice", "admin")
    sched = PasswordResetCleanupScheduler(repo)
    pruned = await sched._prune_once()
    assert pruned == 1
    rows = await db.fetchall("SELECT 1 FROM password_reset_tokens")
    assert len(rows) == 1


async def test_start_stop_is_clean(db):
    """``start()`` is idempotent and ``stop()`` exits the task."""
    repo = SqlitePasswordResetRepo(db)
    sched = PasswordResetCleanupScheduler(repo, interval_seconds=0.05)
    await sched.start()
    await sched.start()  # idempotent — second start is a noop
    await asyncio.sleep(0.1)
    await sched.stop()
    # Re-stop is fine.
    await sched.stop()
