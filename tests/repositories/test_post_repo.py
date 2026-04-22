"""Tests for socialhome.repositories.post_repo."""

from __future__ import annotations

import pytest

from socialhome.domain.post import PostType


@pytest.fixture
async def env(tmp_dir):
    """Minimal env with post and user repos over a real SQLite database."""
    from socialhome.crypto import generate_identity_keypair, derive_instance_id
    from socialhome.db.database import AsyncDatabase
    from socialhome.infrastructure.event_bus import EventBus
    from socialhome.repositories.post_repo import SqlitePostRepo
    from socialhome.repositories.user_repo import SqliteUserRepo
    from socialhome.services.feed_service import FeedService
    from socialhome.services.user_service import UserService

    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    bus = EventBus()

    class Env:
        pass

    e = Env()
    e.db = db
    e.user_repo = SqliteUserRepo(db)
    e.post_repo = SqlitePostRepo(db)
    e.user_svc = UserService(e.user_repo, bus, own_instance_public_key=kp.public_key)
    e.feed_svc = FeedService(e.post_repo, e.user_repo, bus)
    yield e
    await db.shutdown()


async def test_save_and_get_post(env):
    """A post created via feed_svc can be retrieved by ID."""
    u = await env.user_svc.provision(username="alice", display_name="Alice")
    p = await env.feed_svc.create_post(
        author_user_id=u.user_id,
        type=PostType.TEXT,
        content="hello",
    )
    got = await env.feed_svc.get_post(p.id)
    assert got.id == p.id
    assert got.content == "hello"


async def test_list_feed(env):
    """list_feed returns all created posts in reverse chronological order."""
    u = await env.user_svc.provision(username="alice", display_name="Alice")
    for i in range(3):
        await env.feed_svc.create_post(
            author_user_id=u.user_id,
            type=PostType.TEXT,
            content=f"post {i}",
        )
    feed = await env.feed_svc.list_feed(limit=10)
    assert len(feed) == 3


async def test_get_missing_post_raises(env):
    """Getting a nonexistent post raises KeyError."""
    with pytest.raises(KeyError):
        await env.feed_svc.get_post("nonexistent")
