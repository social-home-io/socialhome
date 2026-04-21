"""Tests for :class:`UnitOfWork` — transactional batch + buffered events."""

from __future__ import annotations

import pytest

from social_home.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from social_home.db.database import AsyncDatabase
from social_home.db.unit_of_work import UnitOfWork


@pytest.fixture
async def db(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "uow.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    yield db
    await db.shutdown()


class _RecordingBus:
    def __init__(self):
        self.events: list = []

    async def publish(self, event):
        self.events.append(event)


# ─── Commit path ─────────────────────────────────────────────────────────


async def test_writes_and_events_commit_on_clean_exit(db):
    """Buffered writes apply on commit; events fire after commit."""
    bus = _RecordingBus()
    async with UnitOfWork(db, bus=bus) as uow:
        await uow.exec(
            "INSERT INTO users(username, user_id, display_name) VALUES('a','a-id','A')",
        )
        await uow.exec(
            "INSERT INTO users(username, user_id, display_name) VALUES('b','b-id','B')",
        )
        uow.publish({"kind": "user_created", "username": "a"})
        uow.publish({"kind": "user_created", "username": "b"})

    rows = await db.fetchall("SELECT username FROM users ORDER BY username")
    names = [r["username"] for r in rows]
    assert "a" in names and "b" in names
    assert [e["username"] for e in bus.events] == ["a", "b"]


# ─── Rollback path ───────────────────────────────────────────────────────


async def test_writes_and_events_rolled_back_on_exception(db):
    """If the body raises, no writes commit and no events fire."""
    bus = _RecordingBus()
    with pytest.raises(RuntimeError):
        async with UnitOfWork(db, bus=bus) as uow:
            await uow.exec(
                "INSERT INTO users(username, user_id, display_name)"
                " VALUES('c','c-id','C')",
            )
            uow.publish({"kind": "should_not_fire"})
            raise RuntimeError("boom")

    rows = await db.fetchall(
        "SELECT 1 FROM users WHERE username='c'",
    )
    assert rows == []
    assert bus.events == []


# ─── Lifecycle ───────────────────────────────────────────────────────────


async def test_writing_after_close_raises(db):
    """exec/publish after the CM exits is an error, not silent."""
    async with UnitOfWork(db) as uow:
        pass
    with pytest.raises(RuntimeError, match="closed"):
        await uow.exec("SELECT 1")
    with pytest.raises(RuntimeError, match="closed"):
        uow.publish({"x": 1})


async def test_no_bus_means_events_are_dropped_silently(db):
    """A UoW without a bus doesn't crash when publish() is called."""
    async with UnitOfWork(db) as uow:
        uow.publish({"unwatched": True})
        await uow.exec(
            "INSERT INTO users(username, user_id, display_name) VALUES('z','z-id','Z')",
        )
    rows = await db.fetchall("SELECT 1 FROM users WHERE username='z'")
    assert len(rows) == 1


async def test_event_handler_failure_does_not_unwind_commit(db):
    """A failing handler must not roll back already-committed writes."""

    class _ExplodingBus:
        async def publish(self, event):
            raise ValueError("listener exploded")

    async with UnitOfWork(db, bus=_ExplodingBus()) as uow:
        await uow.exec(
            "INSERT INTO users(username, user_id, display_name) VALUES('q','q-id','Q')",
        )
        uow.publish({"x": 1})

    rows = await db.fetchall("SELECT 1 FROM users WHERE username='q'")
    assert len(rows) == 1
