"""Tests for AppPendingSessionPruneScheduler.

Mirrors ``test_replay_cache_scheduler.py``: ``_prune_once`` calls the repo's
``prune_pending_sessions`` and returns its count, and the start/stop lifecycle
is idempotent and safe.
"""

from __future__ import annotations

import asyncio

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.infrastructure.app_pending_session_scheduler import (
    AppPendingSessionPruneScheduler,
)
from socialhome.repositories.app_repo import SqliteAppRepo


class _FakeAppRepo:
    """Records prune calls and returns a canned deleted-count."""

    def __init__(self, deleted: int = 7) -> None:
        self.calls = 0
        self._deleted = deleted

    async def prune_pending_sessions(self, **_kwargs) -> int:
        self.calls += 1
        return self._deleted


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    repo = SqliteAppRepo(db)
    yield db, repo
    await db.shutdown()


async def test_prune_once_calls_repo_and_returns_count():
    repo = _FakeAppRepo(deleted=3)
    sched = AppPendingSessionPruneScheduler(repo)
    n = await sched._prune_once()
    assert n == 3
    assert repo.calls == 1


class _FakeGroups:
    def __init__(self) -> None:
        self.sweeps = 0

    async def sweep(self) -> int:
        self.sweeps += 1
        return 2


async def test_prune_once_also_sweeps_group_tables():
    repo = _FakeAppRepo(deleted=3)
    groups = _FakeGroups()
    sched = AppPendingSessionPruneScheduler(repo, groups=groups)
    assert await sched._prune_once() == 5
    assert groups.sweeps == 1


async def test_double_start_is_idempotent(env):
    _, repo = env
    sched = AppPendingSessionPruneScheduler(repo, interval_seconds=10.0)
    await sched.start()
    await sched.start()  # no-op
    await sched.stop()


async def test_stop_without_start_is_safe(env):
    _, repo = env
    sched = AppPendingSessionPruneScheduler(repo)
    await sched.stop()


async def test_loop_runs_periodically():
    """A quick interval lets the loop tick at least once."""
    repo = _FakeAppRepo()
    sched = AppPendingSessionPruneScheduler(repo, interval_seconds=0.05)
    await sched.start()
    await asyncio.sleep(0.12)
    await sched.stop()
    assert repo.calls >= 1
