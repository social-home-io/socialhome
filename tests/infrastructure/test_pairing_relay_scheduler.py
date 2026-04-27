"""Tests for :class:`PairingRelayRetentionScheduler` (§11.9 sweep)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.infrastructure.pairing_relay_scheduler import (
    PairingRelayRetentionScheduler,
)
from socialhome.repositories.pairing_relay_repo import SqlitePairingRelayRepo


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    class Env:
        pass

    e = Env()
    e.db = db
    e.repo = SqlitePairingRelayRepo(db)
    yield e
    await db.shutdown()


async def _seed(env, *, request_id, status, received_at):
    """Insert a row directly so tests can backdate ``received_at``."""
    await env.db.enqueue(
        """
        INSERT INTO pairing_relay(
            id, from_instance, target_instance_id, message,
            received_at, status
        ) VALUES(?, ?, ?, ?, ?, ?)
        """,
        (request_id, "peer-a", "peer-b", "msg", received_at.isoformat(), status),
    )


async def test_sweep_drops_old_resolved_rows_keeps_recent(env):
    now = datetime.now(timezone.utc)
    sched = PairingRelayRetentionScheduler(
        env.repo,
        pending_window=timedelta(days=30),
        resolved_window=timedelta(days=7),
    )

    await _seed(
        env,
        request_id="old-approved",
        status="approved",
        received_at=now - timedelta(days=10),
    )
    await _seed(
        env,
        request_id="recent-approved",
        status="approved",
        received_at=now - timedelta(days=2),
    )
    await _seed(
        env,
        request_id="old-declined",
        status="declined",
        received_at=now - timedelta(days=8),
    )
    await _seed(
        env,
        request_id="recent-declined",
        status="declined",
        received_at=now - timedelta(hours=1),
    )

    pruned = await sched._prune_once()
    assert pruned == 2

    rows = await env.db.fetchall("SELECT id FROM pairing_relay ORDER BY id")
    surviving = {r["id"] for r in rows}
    assert surviving == {"recent-approved", "recent-declined"}


async def test_sweep_drops_very_old_pending(env):
    now = datetime.now(timezone.utc)
    sched = PairingRelayRetentionScheduler(
        env.repo,
        pending_window=timedelta(days=30),
        resolved_window=timedelta(days=7),
    )

    await _seed(
        env,
        request_id="ancient-pending",
        status="pending",
        received_at=now - timedelta(days=45),
    )
    await _seed(
        env,
        request_id="recent-pending",
        status="pending",
        received_at=now - timedelta(days=10),
    )

    pruned = await sched._prune_once()
    assert pruned == 1

    pending = await env.repo.list_pending()
    assert {p["id"] for p in pending} == {"recent-pending"}


async def test_sweep_is_a_noop_when_table_empty(env):
    sched = PairingRelayRetentionScheduler(env.repo)
    assert await sched._prune_once() == 0


async def test_double_start_is_idempotent(env):
    sched = PairingRelayRetentionScheduler(env.repo, interval_seconds=10.0)
    await sched.start()
    await sched.start()  # no-op
    await sched.stop()


async def test_stop_without_start_is_safe(env):
    sched = PairingRelayRetentionScheduler(env.repo)
    await sched.stop()


async def test_loop_runs_periodically(env):
    """Quick interval lets the loop tick at least once."""
    import asyncio

    sched = PairingRelayRetentionScheduler(env.repo, interval_seconds=0.05)
    await sched.start()
    await asyncio.sleep(0.12)
    await sched.stop()
