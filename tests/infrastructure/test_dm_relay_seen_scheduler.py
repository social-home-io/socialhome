"""Tests for DmRelaySeenPruneScheduler (§12.5.3)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.infrastructure.dm_relay_seen_scheduler import (
    DmRelaySeenPruneScheduler,
)
from socialhome.repositories.dm_routing_repo import SqliteDmRoutingRepo
from socialhome.repositories.federation_repo import SqliteFederationRepo
from socialhome.services.dm_routing_service import DmRoutingService


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    repo = SqliteDmRoutingRepo(db)
    fed_repo = SqliteFederationRepo(db)
    service = DmRoutingService(repo, fed_repo, own_instance_id="me")
    yield db, service
    await db.shutdown()


async def test_prune_once_deletes_old_rows(env):
    """Rows older than DEDUP_TTL_SECONDS are dropped on a sweep."""
    db, service = env
    # Seed one fresh + one stale row directly.  ``mark_seen`` writes
    # ``datetime('now')`` (space separator, no TZ); for the stale row
    # we hand-craft an ISO-with-Z timestamp from the distant past.
    await db.enqueue(
        "INSERT INTO dm_relay_seen(msg_id, seen_at) VALUES('fresh', datetime('now'))",
    )
    stale_iso = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    await db.enqueue(
        "INSERT INTO dm_relay_seen(msg_id, seen_at) VALUES('stale', ?)",
        (stale_iso,),
    )
    sched = DmRelaySeenPruneScheduler(service)
    n = await sched._prune_once()
    assert n == 1
    rows = await db.fetchall("SELECT msg_id FROM dm_relay_seen")
    assert {r["msg_id"] for r in rows} == {"fresh"}


async def test_double_start_is_idempotent(env):
    _, service = env
    sched = DmRelaySeenPruneScheduler(service, interval_seconds=10.0)
    await sched.start()
    await sched.start()  # no-op
    await sched.stop()


async def test_stop_without_start_is_safe(env):
    _, service = env
    sched = DmRelaySeenPruneScheduler(service)
    await sched.stop()


async def test_loop_runs_periodically(env):
    """Quick interval lets the loop tick at least once."""
    _, service = env
    sched = DmRelaySeenPruneScheduler(service, interval_seconds=0.05)
    await sched.start()
    await asyncio.sleep(0.12)
    await sched.stop()
