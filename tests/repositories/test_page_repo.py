"""Tests for socialhome.repositories.page_repo."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.repositories.page_repo import (
    PageLockError,
    PageNotFoundError,
    PageVersion,
    SqlitePageRepo,
    new_page,
)


@pytest.fixture
async def env(tmp_dir):
    """Minimal env with a page repo over a real SQLite database."""
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
    e.iid = iid
    e.page_repo = SqlitePageRepo(db)
    yield e
    await db.shutdown()


async def test_page_space_scope(env):
    """Space pages are stored separately from household pages."""
    from socialhome.crypto import generate_identity_keypair as _gkp

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
    await env.page_repo.save(household, space_id=None)
    await env.page_repo.save(space_page, space_id=sp_id)

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
    await env.page_repo.save(page, space_id=None)

    await env.page_repo.request_delete(page.id, "u1")
    got = await env.page_repo.get(page.id)
    assert got.delete_requested_by == "u1"

    await env.page_repo.approve_delete(page.id, "u2")
    got2 = await env.page_repo.get(page.id)
    assert got2.delete_approved_by == "u2"

    await env.page_repo.delete(page.id, space_id=None)
    assert await env.page_repo.get(page.id) is None


async def test_page_lock_missing(env):
    """Locking a nonexistent page raises PageNotFoundError."""
    with pytest.raises(PageNotFoundError):
        await env.page_repo.acquire_lock("nonexistent-id", "anna")


async def test_page_expired_lock_release(env):
    """Expired locks get cleaned up by release_expired_locks."""
    page = new_page(title="Locked", content="x", created_by="u1")
    await env.page_repo.save(page, space_id=None)

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
    await env.page_repo.save(p, space_id=None)
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


async def _snapshot_count(env, page_id: str) -> int:
    row = await env.db.fetchone(
        "SELECT COUNT(*) AS n FROM space_page_snapshots WHERE page_id=?",
        (page_id,),
    )
    return int(row["n"])


async def test_insert_snapshot_caps_per_page(env, monkeypatch):
    """``insert_snapshot`` prunes to the newest MAX_PAGE_SNAPSHOTS per page."""
    import socialhome.repositories.page_repo as mod

    monkeypatch.setattr(mod, "MAX_PAGE_SNAPSHOTS", 3)
    p = new_page(title="Wiki", content="x", created_by="u1")
    await env.page_repo.save(p, space_id=None)
    for i in range(5):
        await env.page_repo.insert_snapshot(
            page_id=p.id,
            space_id=None,
            body=f"body-{i}",
            author_user_id="u1",
            side="base",
            conflict=False,
        )
    assert await _snapshot_count(env, p.id) == 3
    # Newest 3 by snapshot_at survive.
    rows = await env.db.fetchall(
        "SELECT body FROM space_page_snapshots WHERE page_id=? "
        "ORDER BY snapshot_at ASC",
        (p.id,),
    )
    assert [r["body"] for r in rows] == ["body-2", "body-3", "body-4"]


async def test_delete_removes_page_snapshots(env):
    """``delete`` also drops the page's snapshot rows."""
    p = new_page(title="Wiki", content="x", created_by="u1")
    await env.page_repo.save(p, space_id=None)
    await env.page_repo.insert_snapshot(
        page_id=p.id,
        space_id=None,
        body="b",
        author_user_id="u1",
        side="base",
        conflict=False,
    )
    assert await _snapshot_count(env, p.id) == 1
    await env.page_repo.delete(p.id, space_id=None)
    assert await _snapshot_count(env, p.id) == 0


# ─── §24.11 cross-space scoping (issue #693) ──────────────────────────────


async def _mk_space(db, space_id):
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?,?,?,?,?)",
        (space_id, space_id, "inst-1", "owner", "ab" * 32),
    )


@pytest.fixture
async def scoped(env):
    """Pages in space A and space B plus a household page."""
    await _mk_space(env.db, "space-a")
    await _mk_space(env.db, "space-b")
    from socialhome.domain.page import Page

    for sid, pid in (("space-a", "pg-a"), ("space-b", "pg-b"), (None, "pg-hh")):
        await env.page_repo.save(
            Page(
                id=pid,
                title=f"title-{pid}",
                content=f"body-{pid}",
                created_by="uid-owner",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
                space_id=sid,
            ),
            space_id=sid,
        )
    return env


async def _page_row(env, table, page_id):
    row = await env.db.fetchone(f"SELECT * FROM {table} WHERE id=?", (page_id,))
    return dict(row) if row is not None else None


async def test_page_save_refuses_cross_space_id(scoped):
    """Re-saving space B's page id under space A leaves B untouched."""
    from socialhome.domain.page import Page

    before = await _page_row(scoped, "space_pages", "pg-b")
    stolen = Page(
        id="pg-b",
        title="stolen",
        content="stolen body",
        created_by="uid-attacker",
        created_at="2026-02-01T00:00:00+00:00",
        updated_at="2026-02-01T00:00:00+00:00",
        space_id="space-a",
    )
    assert await scoped.page_repo.save(stolen, space_id="space-a") is False
    assert await _page_row(scoped, "space_pages", "pg-b") == before
    assert [p.id for p in await scoped.page_repo.list(space_id="space-a")] == ["pg-a"]


async def test_page_save_own_space_still_works(scoped):
    """The same upsert scoped to the page's own space applies."""
    from socialhome.domain.page import Page

    ok = await scoped.page_repo.save(
        Page(
            id="pg-b",
            title="edited",
            content="edited body",
            created_by="uid-owner",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-02-01T00:00:00+00:00",
            space_id="space-b",
        ),
        space_id="space-b",
    )
    assert ok is True
    assert (await scoped.page_repo.get("pg-b")).title == "edited"


async def test_page_delete_refuses_cross_space_id(scoped):
    """A delete routed as space A cannot remove space B's page."""
    assert await scoped.page_repo.delete("pg-b", space_id="space-a") is False
    assert await scoped.page_repo.get("pg-b") is not None
    assert await scoped.page_repo.delete("pg-b", space_id="space-b") is True
    assert await scoped.page_repo.get("pg-b") is None


async def test_space_page_delete_cannot_reach_the_personal_pages_table(scoped):
    """A space-routed delete naming a household page id spares ``pages``.

    The sharpest shape of issue #693: ``delete`` used to fire at *both*
    tables, so a ``SPACE_PAGE_DELETED`` naming a household page id wiped
    the household's personal page.
    """
    before = await _page_row(scoped, "pages", "pg-hh")
    assert before is not None
    assert await scoped.page_repo.delete("pg-hh", space_id="space-a") is False
    assert await scoped.page_repo.delete("pg-hh", space_id="space-b") is False
    assert await _page_row(scoped, "pages", "pg-hh") == before


async def test_space_page_save_cannot_reach_the_personal_pages_table(scoped):
    """A space-scoped upsert naming a household page id never writes ``pages``."""
    from socialhome.domain.page import Page

    before = await _page_row(scoped, "pages", "pg-hh")
    assert (
        await scoped.page_repo.save(
            Page(
                id="pg-hh",
                title="stolen",
                content="stolen",
                created_by="uid-attacker",
                created_at="2026-02-01T00:00:00+00:00",
                updated_at="2026-02-01T00:00:00+00:00",
                space_id="space-a",
            ),
            space_id="space-a",
        )
        is True
    )  # lands as a *new* space_pages row, the household row is untouched
    assert await _page_row(scoped, "pages", "pg-hh") == before


async def test_page_delete_drops_only_its_own_space_snapshots(scoped):
    """Snapshot cleanup is scoped to the same space as the delete."""
    for sid, pid in (("space-a", "pg-a"), ("space-b", "pg-b")):
        await scoped.page_repo.insert_snapshot(
            page_id=pid,
            space_id=sid,
            body="snap",
            author_user_id="uid-owner",
            side="base",
            conflict=False,
        )
    assert await scoped.page_repo.delete("pg-b", space_id="space-a") is False
    rows = await scoped.db.fetchall(
        "SELECT page_id FROM space_page_snapshots ORDER BY page_id"
    )
    assert [r["page_id"] for r in rows] == ["pg-a", "pg-b"]
    assert await scoped.page_repo.delete("pg-b", space_id="space-b") is True
    rows = await scoped.db.fetchall("SELECT page_id FROM space_page_snapshots")
    assert [r["page_id"] for r in rows] == ["pg-a"]
