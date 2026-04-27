"""Tests for :class:`DmGcScheduler` (§23.47c)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.conversation import Conversation, ConversationType
from socialhome.infrastructure.dm_gc_scheduler import DmGcScheduler
from socialhome.repositories.conversation_repo import SqliteConversationRepo


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
    # Two local users for membership rows.
    for username in ("alice", "bob"):
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (username, f"uid-{username}", username.title()),
        )

    class Env:
        pass

    e = Env()
    e.db = db
    e.iid = iid
    e.repo = SqliteConversationRepo(db)
    yield e
    await db.shutdown()


async def _make_dm_with_members(env, *, conv_id: str, members: list[str]) -> None:
    now = datetime.now(timezone.utc)
    conv = Conversation(
        id=conv_id,
        type=ConversationType.DM if len(members) == 2 else ConversationType.GROUP_DM,
        created_at=now,
    )
    await env.repo.create(conv)
    from socialhome.domain.conversation import ConversationMember

    for m in members:
        await env.repo.add_member(
            ConversationMember(
                conversation_id=conv_id,
                username=m,
                joined_at=now.isoformat(),
            )
        )


async def test_sweep_hard_deletes_fully_left_dm(env):
    sched = DmGcScheduler(env.repo)
    await _make_dm_with_members(env, conv_id="c-1", members=["alice", "bob"])
    await env.repo.soft_leave("c-1", "alice")
    await env.repo.soft_leave("c-1", "bob")

    pruned = await sched._sweep_once()
    assert pruned == 1
    assert await env.repo.get("c-1") is None


async def test_sweep_keeps_dm_with_remaining_member(env):
    sched = DmGcScheduler(env.repo)
    await _make_dm_with_members(env, conv_id="c-2", members=["alice", "bob"])
    await env.repo.soft_leave("c-2", "alice")  # only one party left

    pruned = await sched._sweep_once()
    assert pruned == 0
    assert await env.repo.get("c-2") is not None


async def test_sweep_skips_conversation_with_remote_member(env):
    """Federated conversations are owned by the peer; we don't GC them."""
    sched = DmGcScheduler(env.repo)
    await _make_dm_with_members(env, conv_id="c-3", members=["alice", "bob"])
    await env.repo.soft_leave("c-3", "alice")
    await env.repo.soft_leave("c-3", "bob")
    await env.db.enqueue(
        "INSERT INTO conversation_remote_members(conversation_id, instance_id,"
        " remote_username) VALUES(?, ?, ?)",
        ("c-3", "peer-x", "carol"),
    )

    pruned = await sched._sweep_once()
    assert pruned == 0
    assert await env.repo.get("c-3") is not None


async def test_sweep_is_idempotent_on_empty(env):
    sched = DmGcScheduler(env.repo)
    assert await sched._sweep_once() == 0


async def test_double_start_is_idempotent(env):
    sched = DmGcScheduler(env.repo, interval_seconds=10.0)
    await sched.start()
    await sched.start()  # no-op
    await sched.stop()


async def test_stop_without_start_is_safe(env):
    sched = DmGcScheduler(env.repo)
    await sched.stop()


async def test_loop_runs_periodically(env):
    """Quick interval lets the loop tick at least once."""
    import asyncio

    sched = DmGcScheduler(env.repo, interval_seconds=0.05)
    await sched.start()
    await asyncio.sleep(0.12)
    await sched.stop()
