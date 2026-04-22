"""Tests for socialhome.repositories.outbox_repo and infrastructure.outbox_processor."""

from __future__ import annotations

import asyncio

import pytest

from socialhome.domain.federation import FederationEventType
from socialhome.infrastructure.outbox_processor import (
    BACKOFF_SECONDS,
    MAX_ATTEMPTS,
    OutboxProcessor,
)
from socialhome.repositories.outbox_repo import SqliteOutboxRepo


@pytest.fixture
async def env(tmp_dir):
    """Minimal env with an outbox repo over a real SQLite database."""
    from socialhome.crypto import generate_identity_keypair, derive_instance_id
    from socialhome.db.database import AsyncDatabase

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
    e.outbox_repo = SqliteOutboxRepo(db)
    yield e
    await db.shutdown()


async def test_outbox_full_cycle(env):
    """Enqueued entry appears in list_due; marking delivered removes it from due list."""
    eid = await env.outbox_repo.enqueue(
        instance_id="peer",
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload_json="{}",
    )
    due = await env.outbox_repo.list_due()
    assert len(due) == 1
    await env.outbox_repo.mark_delivered(eid)
    assert await env.outbox_repo.list_due() == []


async def test_outbox_processor_backoff(env):
    """Backoff schedule starts at 5s and caps at 4 hours."""
    assert BACKOFF_SECONDS[0] == 5
    assert BACKOFF_SECONDS[1] == 10
    assert BACKOFF_SECONDS[-1] == 14400


async def test_outbox_processor_max_attempts(env):
    """Entry transitions to 'failed' after MAX_ATTEMPTS delivery failures."""
    eid = await env.outbox_repo.enqueue(
        instance_id="peer-x",
        event_type=FederationEventType.DM_MESSAGE,
        payload_json="{}",
    )
    await env.db.enqueue(
        "UPDATE federation_outbox SET attempts=? WHERE id=?",
        (MAX_ATTEMPTS - 1, eid),
    )

    async def always_fail(entry):
        return False

    proc = OutboxProcessor(env.outbox_repo, always_fail, rng=lambda: 0.5)
    await proc.drain_once()

    rows = await env.db.fetchall(
        "SELECT status FROM federation_outbox WHERE id=?", (eid,)
    )
    assert rows[0]["status"] == "failed"


async def test_outbox_processor_exception_retry(env):
    """A delivery callback that raises is treated as a retry, not a crash."""
    eid = await env.outbox_repo.enqueue(
        instance_id="peer-x",
        event_type=FederationEventType.DM_MESSAGE,
        payload_json="{}",
    )

    async def raise_always(entry):
        raise RuntimeError("network error")

    proc = OutboxProcessor(env.outbox_repo, raise_always, rng=lambda: 0.5)
    count = await proc.drain_once()
    assert count == 1

    rows = await env.db.fetchall(
        "SELECT status, attempts FROM federation_outbox WHERE id=?", (eid,)
    )
    assert rows[0]["status"] == "pending"
    assert int(rows[0]["attempts"]) == 1


async def test_outbox_processor_lifecycle(env):
    """OutboxProcessor start and stop do not raise."""

    async def noop(entry):
        return True

    proc = OutboxProcessor(
        env.outbox_repo,
        noop,
        poll_interval_seconds=0.01,
        rng=lambda: 0.5,
    )
    await proc.start()
    await asyncio.sleep(0.05)
    await proc.stop()


async def test_outbox_processor_drain(env):
    """drain_once delivers a pending entry and calls the callback exactly once."""
    eid = await env.outbox_repo.enqueue(
        instance_id="peer",
        event_type=FederationEventType.DM_MESSAGE,
        payload_json="{}",
    )
    delivered = []

    async def deliver(entry):
        delivered.append(entry.id)
        return True

    proc = OutboxProcessor(env.outbox_repo, deliver, rng=lambda: 0.5)
    count = await proc.drain_once()
    assert count == 1 and delivered == [eid]
