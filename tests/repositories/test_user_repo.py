"""Tests for social_home.repositories.user_repo."""

from __future__ import annotations

import pytest


@pytest.fixture
async def env(tmp_dir):
    """Minimal env with a user repo over a real SQLite database."""
    from social_home.crypto import generate_identity_keypair, derive_instance_id
    from social_home.db.database import AsyncDatabase
    from social_home.infrastructure.event_bus import EventBus
    from social_home.repositories.user_repo import SqliteUserRepo
    from social_home.services.user_service import UserService

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
    e.user_repo = SqliteUserRepo(db)
    e.user_svc = UserService(
        e.user_repo, EventBus(), own_instance_public_key=kp.public_key
    )
    yield e
    await db.shutdown()


async def test_save_and_get_by_username(env):
    """A provisioned user can be retrieved by username."""
    u = await env.user_svc.provision(username="alice", display_name="Alice")
    got = await env.user_repo.get("alice")
    assert got is not None
    assert got.user_id == u.user_id


async def test_get_missing_user_returns_none(env):
    """Getting a non-existent username returns None."""
    got = await env.user_repo.get("nobody")
    assert got is None


async def test_list_active_users(env):
    """list_active returns all users with active state."""
    await env.user_svc.provision(username="alice", display_name="Alice")
    await env.user_svc.provision(username="bob", display_name="Bob")
    users = await env.user_svc.list_active()
    assert len(users) == 2


async def test_get_by_user_id(env):
    """get_by_user_id returns the user for a known user_id."""
    u = await env.user_svc.provision(username="alice", display_name="Alice")
    got = await env.user_svc.get_by_user_id(u.user_id)
    assert got.username == "alice"
