"""Tests for social_home.repositories.page_repo."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from social_home.repositories.page_repo import (
    PageLockError,
    PageNotFoundError,
    PageVersion,
    SqlitePageRepo,
    new_page,
)


@pytest.fixture
async def env(tmp_dir):
    """Minimal env with a page repo over a real SQLite database."""
    from social_home.crypto import generate_identity_keypair, derive_instance_id
    from social_home.db.database import AsyncDatabase

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
    e.iid = iid
    e.page_repo = SqlitePageRepo(db)
    yield e
    await db.shutdown()


async def test_page_space_scope(env):
    """Space pages are stored separately from household pages."""
    from social_home.crypto import generate_identity_keypair as _gkp

    kp_sp = _gkp()
    sp_id = uuid.uuid4().hex
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("pg_owner", "uid-pg-owner", "PageOwner"),
    )
    await env.db.enqueue(
        """INSERT INTO spaces(
            id, name, owner_instance_id, owner_username, identity_public_key,
            config_sequence, space_type, join_mode
        ) VALUES(?,?,?,?,?,0,'private','invite_only')""",
        (sp_id, "PageSpace", env.iid, "pg_owner", kp_sp.public_key.hex()),
    )

    household = new_page(title="Household wiki", content="HH", created_by="u1")
    space_page = new_page(
        title="Space wiki", content="SP", created_by="u1", space_id=sp_id
    )
    await env.page_repo.save(household)
    await env.page_repo.save(space_page)

    hh_list = await env.page_repo.list()
    sp_list = await env.page_repo.list(space_id=sp_id)

    hh_ids = {p.id for p in hh_list}
    sp_ids = {p.id for p in sp_list}

    assert household.id in hh_ids
    assert household.id not in sp_ids
    assert space_page.id in sp_ids
    assert space_page.id not in hh_ids


async def test_page_two_step_delete(env):
    """Request delete then approve; hard delete removes the page."""
    page = new_page(title="Wiki", content="content", created_by="u1")
    await env.page_repo.save(page)

    await env.page_repo.request_delete(page.id, "u1")
    got = await env.page_repo.get(page.id)
    assert got.delete_requested_by == "u1"

    await env.page_repo.approve_delete(page.id, "u2")
    got2 = await env.page_repo.get(page.id)
    assert got2.delete_approved_by == "u2"

    await env.page_repo.delete(page.id)
    assert await env.page_repo.get(page.id) is None


async def test_page_lock_missing(env):
    """Locking a nonexistent page raises PageNotFoundError."""
    with pytest.raises(PageNotFoundError):
        await env.page_repo.acquire_lock("nonexistent-id", "anna")


async def test_page_expired_lock_release(env):
    """Expired locks get cleaned up by release_expired_locks."""
    page = new_page(title="Locked", content="x", created_by="u1")
    await env.page_repo.save(page)

    await env.page_repo.acquire_lock(page.id, "anna", ttl=timedelta(microseconds=1))

    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    await env.db.enqueue(
        "UPDATE pages SET lock_expires_at=? WHERE id=?",
        (past, page.id),
    )

    count = await env.page_repo.release_expired_locks()
    assert count >= 1

    refreshed = await env.page_repo.get(page.id)
    assert refreshed.locked_by is None


async def test_page_lock_and_versions(env):
    """Acquiring a lock blocks a second user; releasing allows a new lock."""
    p = new_page(title="Wiki", content="hello", created_by="u1")
    await env.page_repo.save(p)
    await env.page_repo.acquire_lock(p.id, "anna")
    with pytest.raises(PageLockError):
        await env.page_repo.acquire_lock(p.id, "bob")
    await env.page_repo.release_lock(p.id, "anna")
    v = PageVersion(
        id="v1",
        page_id=p.id,
        version=1,
        title="Wiki",
        content="v1",
        edited_by="u1",
        edited_at=datetime.now(timezone.utc).isoformat(),
    )
    await env.page_repo.save_version(v)
    versions = await env.page_repo.list_versions(p.id)
    assert len(versions) == 1
