"""Tests for socialhome.repositories.page_repo."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.federation.owner_bound_id import (
    SPACE_PAGE_KIND,
    OwnerBinding,
    check_owner_bound_id,
    is_owner_bound,
)
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
    versions = await env.page_repo.list_versions(p.id, space_id=None)
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
            side="mine",
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
        side="mine",
        conflict=False,
    )
    assert await _snapshot_count(env, p.id) == 1
    await env.page_repo.delete(p.id, space_id=None)
    assert await _snapshot_count(env, p.id) == 0


# ─── §24.11 cross-space scoping ──────────────────────────────


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

    The sharpest shape of the unscoped write: ``delete`` used to fire at *both*
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
            side="mine",
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


def test_a_space_page_id_commits_to_its_creator():
    """v_36: a space page federates, so its id is owner-bound to its
    creator in its space; a household page keeps a plain id."""
    page = new_page(title="W", content="x", created_by="u1", space_id="sp-1")
    assert (
        check_owner_bound_id(
            SPACE_PAGE_KIND, page.id, space_id="sp-1", owner_user_id="u1"
        )
        is OwnerBinding.VALID
    )
    assert (
        check_owner_bound_id(
            SPACE_PAGE_KIND, page.id, space_id="sp-1", owner_user_id="u2"
        )
        is OwnerBinding.MISMATCH
    )
    assert not is_owner_bound(new_page(title="H", content="x", created_by="u1").id)


# ─── Household-only surface: locks + two-step delete never reach a space page


async def _seed_space_row(env, sid: str) -> None:
    from socialhome.crypto import generate_identity_keypair as _gkp

    await env.db.enqueue(
        "INSERT OR IGNORE INTO users(username, user_id, display_name)"
        " VALUES('pg_owner', 'uid-pg-owner', 'PageOwner')",
    )
    await env.db.enqueue(
        """INSERT INTO spaces(
            id, name, owner_instance_id, owner_username, identity_public_key,
            config_sequence, space_type, join_mode
        ) VALUES(?,?,?,?,?,0,'private','invite_only')""",
        (sid, "S", env.iid, "pg_owner", _gkp().public_key.hex()),
    )


async def test_get_household_page_never_returns_a_space_page(env):
    await _seed_space_row(env, "sp-h")
    sp_page = new_page(title="S", content="x", created_by="u1", space_id="sp-h")
    await env.page_repo.save(sp_page, space_id="sp-h")
    hh = new_page(title="H", content="y", created_by="u1")
    await env.page_repo.save(hh, space_id=None)
    assert await env.page_repo.get_household_page(sp_page.id) is None
    got = await env.page_repo.get_household_page(hh.id)
    assert got is not None and got.space_id is None


async def test_lock_and_delete_request_ignore_space_pages(env):
    await _seed_space_row(env, "sp-h")
    sp_page = new_page(title="S", content="x", created_by="u1", space_id="sp-h")
    await env.page_repo.save(sp_page, space_id="sp-h")
    with pytest.raises(PageNotFoundError):
        await env.page_repo.acquire_lock(sp_page.id, "anna")
    with pytest.raises(PageNotFoundError):
        await env.page_repo.refresh_lock(sp_page.id, "anna")
    assert await env.page_repo.get_lock(sp_page.id) is None
    await env.page_repo.request_delete(sp_page.id, "anna")
    await env.page_repo.approve_delete(sp_page.id, "bob")
    row = await env.db.fetchone(
        "SELECT locked_by, delete_requested_by, delete_approved_by"
        " FROM space_pages WHERE id=?",
        (sp_page.id,),
    )
    assert tuple(row) == (None, None, None)


@pytest.mark.parametrize("sid", ["space-a", None])
async def test_page_update_stores_the_writers_updated_at(scoped, sid):
    """An update keeps the stamp the writer passed — one tz-aware ISO shape
    for the column, and exactly what the editor's next ``base_updated_at``
    has to match (``datetime('now')`` used to overwrite it, naive). An
    empty stamp becomes now, tz-aware."""
    from dataclasses import replace

    pid = "pg-a" if sid else "pg-hh"
    table = "space_pages" if sid else "pages"
    page = (
        await scoped.page_repo.get_space_page(pid, space_id=sid)
        if sid
        else await scoped.page_repo.get_household_page(pid)
    )
    stamp = "2026-02-03T04:05:06.123456+00:00"
    assert await scoped.page_repo.save(
        replace(page, content="v2", updated_at=stamp), space_id=sid
    )
    assert (await _page_row(scoped, table, pid))["updated_at"] == stamp
    assert await scoped.page_repo.save(
        replace(page, content="v3", updated_at=""), space_id=sid
    )
    stored = (await _page_row(scoped, table, pid))["updated_at"]
    assert datetime.fromisoformat(stored).tzinfo is not None


# ─── v_48: history cap + conflict sides ───────────────────────────────


async def _snap(repo, *, pid, sid, body, conflict, title=None, by="u1"):
    await repo.insert_snapshot(
        page_id=pid,
        space_id=sid,
        body=body,
        author_user_id=by,
        side="mine",
        conflict=conflict,
        title=title,
    )


async def test_snapshot_prune_never_drops_an_open_conflict_side(scoped, monkeypatch):
    """The prune keeps the newest RESOLVED rows; an open side (conflict=1)
    is never pruned, however many resolved rows follow it."""
    import socialhome.repositories.page_repo as mod

    monkeypatch.setattr(mod, "MAX_PAGE_SNAPSHOTS", 2)
    repo = scoped.page_repo
    await _snap(repo, pid="pg-a", sid="space-a", body="open", conflict=True, title="T")
    for i in range(4):
        await _snap(repo, pid="pg-a", sid="space-a", body=f"old-{i}", conflict=False)
    sides = await repo.list_conflict_sides("pg-a", space_id="space-a")
    assert [s.content for s in sides] == ["open"]
    rows = await scoped.db.fetchall(
        "SELECT body FROM space_page_snapshots WHERE conflict=0 ORDER BY snapshot_at"
    )
    assert [r["body"] for r in rows] == ["old-2", "old-3"]


async def test_snapshot_prune_is_scoped_to_its_space(scoped, monkeypatch):
    """Rows of another scope sharing the page id never count against, or
    get pruned by, this scope's cap."""
    import socialhome.repositories.page_repo as mod

    monkeypatch.setattr(mod, "MAX_PAGE_SNAPSHOTS", 1)
    repo = scoped.page_repo
    await _snap(repo, pid="shared", sid="space-b", body="b-row", conflict=False)
    for i in range(3):
        await _snap(repo, pid="shared", sid="space-a", body=f"a-{i}", conflict=False)
    rows = await scoped.db.fetchall(
        "SELECT space_id, body FROM space_page_snapshots WHERE page_id='shared'"
        " ORDER BY space_id"
    )
    assert [(r["space_id"], r["body"]) for r in rows] == [
        ("space-a", "a-2"),
        ("space-b", "b-row"),
    ]


async def test_conflict_sides_round_trip_with_their_titles(scoped):
    from socialhome.domain.page_version import version_hash

    repo = scoped.page_repo
    await _snap(
        repo, pid="pg-a", sid="space-a", body="one", conflict=True, title="T1", by="u1"
    )
    await _snap(
        repo, pid="pg-a", sid="space-a", body="two", conflict=True, title="T2", by="u2"
    )
    sides = await repo.list_conflict_sides("pg-a", space_id="space-a")
    assert [(s.title, s.content, s.by) for s in sides] == [
        ("T1", "one", "u1"),
        ("T2", "two", "u2"),
    ]
    assert sides[0].hash == version_hash("T1", "one")
    # Another space sees none of them.
    assert await repo.list_conflict_sides("pg-a", space_id="space-b") == []
    assert await repo.space_pages_in_conflict("space-a") == {"pg-a"}
    assert await repo.space_pages_in_conflict("space-b") == set()


async def test_a_legacy_bare_side_reads_under_the_page_title(scoped):
    repo = scoped.page_repo
    await _snap(repo, pid="pg-a", sid="space-a", body="{not json", conflict=True)
    await _snap(repo, pid="pg-a", sid="space-a", body='{"x": 1}', conflict=True)
    sides = await repo.list_conflict_sides("pg-a", space_id="space-a")
    assert [(s.title, s.content) for s in sides] == [
        ("title-pg-a", "{not json"),
        ("title-pg-a", '{"x": 1}'),
    ]


async def test_set_conflict_sides_is_exact_and_atomic(scoped):
    from socialhome.domain.page_version import PageConflictSide, version_hash

    repo = scoped.page_repo

    def side(content, at, by="u1", base_seq=0, cover=None):
        return PageConflictSide(
            hash=version_hash("T", content, cover),
            title="T",
            content=content,
            by=by,
            at=at,
            cover_image_url=cover,
            base_seq=base_seq,
        )

    one, two = (
        side("one", "t1", base_seq=3, cover="/c.webp"),
        side("two", "t2", by="u2"),
    )
    await repo.set_conflict_sides("pg-a", space_id="space-a", sides=[one, two])
    assert await repo.list_conflict_sides("pg-a", space_id="space-a") == [one, two]
    # Replacing keeps exactly the new set — the same ``at`` may come back.
    await repo.set_conflict_sides("pg-a", space_id="space-a", sides=[two])
    assert await repo.list_conflict_sides("pg-a", space_id="space-a") == [two]
    await repo.clear_conflict_flag("pg-a", space_id="space-a")
    assert not await repo.has_active_conflict("pg-a", space_id="space-a")


async def test_a_draft_base_round_trips_and_is_never_pruned(scoped, monkeypatch):
    import socialhome.repositories.page_repo as mod
    from socialhome.domain.page_version import DraftBase

    monkeypatch.setattr(mod, "MAX_PAGE_SNAPSHOTS", 1)
    repo = scoped.page_repo
    base = DraftBase(
        title="T", content="c", seq=4, by="u1", cover_image_url="/x", resolves=("h",)
    )
    await repo.set_draft_base("pg-a", space_id="space-a", base=base)
    for i in range(3):
        await _snap(repo, pid="pg-a", sid="space-a", body=f"old-{i}", conflict=False)
    assert await repo.get_draft_base("pg-a", space_id="space-a") == base
    await repo.set_draft_base("pg-a", space_id="space-a", base=replace_seq(base, 5))
    assert (await repo.get_draft_base("pg-a", space_id="space-a")).seq == 5
    await repo.clear_draft_base("pg-a", space_id="space-a")
    assert await repo.get_draft_base("pg-a", space_id="space-a") is None


def replace_seq(base, seq):
    from dataclasses import replace

    return replace(base, seq=seq)


async def test_seq_and_pending_base_seq_round_trip(scoped):
    from dataclasses import replace

    repo = scoped.page_repo
    page = await repo.get_space_page("pg-a", space_id="space-a")
    assert (page.seq, page.pending_base_seq) == (0, None)
    await repo.save(replace(page, seq=7, pending_base_seq=6), space_id="space-a")
    page = await repo.get_space_page("pg-a", space_id="space-a")
    assert (page.seq, page.pending_base_seq) == (7, 6)
    assert await repo.list_pending_drafts() == [("space-a", "pg-a")]
    assert await repo.list_pending_drafts(space_id="space-b") == []
    household = await repo.get_household_page("pg-hh")
    assert (household.seq, household.pending_base_seq) == (0, None)


def _version(pid, sid, n):
    return PageVersion(
        id=uuid.uuid4().hex,
        page_id=pid,
        version=n,
        title="T",
        content=f"v{n}",
        edited_by="u1",
        edited_at=f"2026-01-01T00:00:{n:02d}+00:00",
        space_id=sid,
    )


async def test_space_page_history_keeps_fifty_household_five(scoped):
    from socialhome.repositories.page_repo import MAX_HISTORY, MAX_SPACE_HISTORY

    repo = scoped.page_repo
    assert (MAX_HISTORY, MAX_SPACE_HISTORY) == (5, 50)
    for n in range(1, 56):
        await repo.save_version(_version("pg-a", "space-a", n))
    for n in range(1, 26):
        await repo.save_version(_version("pg-hh", None, 100 + n))
    space_rows = await repo.list_versions("pg-a", space_id="space-a")
    assert [v.version for v in space_rows] == list(range(6, 56))
    household = await repo.list_versions("pg-hh", space_id=None)
    assert [v.version for v in household] == list(range(121, 126))


async def test_history_prune_is_scoped_to_its_space(scoped, monkeypatch):
    import socialhome.repositories.page_repo as mod

    monkeypatch.setattr(mod, "MAX_SPACE_HISTORY", 1)
    repo = scoped.page_repo
    await repo.save_version(_version("shared", "space-b", 1))
    await repo.save_version(_version("shared", "space-a", 2))
    await repo.save_version(_version("shared", "space-a", 3))
    assert [
        v.version for v in await repo.list_versions("shared", space_id="space-b")
    ] == [1]
    assert [
        v.version for v in await repo.list_versions("shared", space_id="space-a")
    ] == [3]


async def test_commit_version_is_atomic_and_scoped(scoped):
    from dataclasses import replace

    from socialhome.domain.page_version import PageConflictSide, version_hash

    repo = scoped.page_repo
    page = await repo.get_space_page("pg-a", space_id="space-a")
    side = PageConflictSide(
        hash=version_hash("T", "s"), title="T", content="s", by="u", at="t1"
    )
    history = [_version("pg-a", "space-a", 0), _version("pg-a", "space-a", 0)]
    assert await repo.commit_version(
        replace(page, content="new", seq=2),
        space_id="space-a",
        history=history,
        sides=[side],
    )
    got = await repo.get_space_page("pg-a", space_id="space-a")
    assert (got.content, got.seq) == ("new", 2)
    assert [
        v.version for v in await repo.list_versions("pg-a", space_id="space-a")
    ] == [1, 2]
    assert await repo.list_conflict_sides("pg-a", space_id="space-a") == [side]
    # Another space's id: nothing written, history rolled back too.
    other = await repo.get_space_page("pg-b", space_id="space-b")
    assert not await repo.commit_version(
        replace(other, content="stolen"),
        space_id="space-a",
        history=[_version("pg-b", "space-a", 0)],
        sides=[],
    )
    assert (
        await repo.get_space_page("pg-b", space_id="space-b")
    ).content == "body-pg-b"
    assert await repo.list_versions("pg-b", space_id="space-a") == []


# ─── Tombstones (migration 0073) ─────────────────────────────────────────


async def test_a_space_delete_keeps_a_blank_tombstone(scoped):
    """A space page delete keeps the row as a content-free tombstone that
    every live read skips; its history and snapshots are gone."""
    repo = scoped.page_repo
    await repo.save_version(_version("pg-a", "space-a", 1))
    await repo.insert_snapshot(
        page_id="pg-a",
        space_id="space-a",
        body="side",
        author_user_id="u",
        side="theirs",
        conflict=True,
    )
    assert await repo.delete("pg-a", space_id="space-a", deleted_by="u-del") is True
    assert await repo.get("pg-a") is None
    assert await repo.get_space_page("pg-a", space_id="space-a") is None
    assert await repo.list(space_id="space-a") == []
    assert await repo.list_since("space-a", "1970-01-01T00:00:00+00:00") == []
    assert await repo.is_page_deleted("pg-a", space_id="space-a") is True
    assert await repo.is_page_deleted("pg-a", space_id="space-b") is False
    row = await _page_row(scoped, "space_pages", "pg-a")
    assert (row["title"], row["content"], row["deleted_by"]) == ("", "", "u-del")
    assert row["deleted_at"] is not None
    assert await repo.list_versions("pg-a", space_id="space-a") == []
    assert await repo.list_conflict_sides("pg-a", space_id="space-a") == []
    # A second delete is a no-op.
    assert await repo.delete("pg-a", space_id="space-a") is False


async def test_a_tombstone_is_never_brought_back_by_an_upsert(scoped):
    from dataclasses import replace

    repo = scoped.page_repo
    page = await repo.get_space_page("pg-a", space_id="space-a")
    await repo.delete("pg-a", space_id="space-a")
    assert await repo.save(replace(page, seq=9), space_id="space-a") is False
    assert not await repo.commit_version(
        replace(page, seq=9),
        space_id="space-a",
        history=[_version("pg-a", "space-a", 0)],
        sides=[],
    )
    assert await repo.get_space_page("pg-a", space_id="space-a") is None
    assert await repo.list_versions("pg-a", space_id="space-a") == []


async def test_a_pending_draft_tombstoned_is_no_longer_pending(scoped):
    from dataclasses import replace

    repo = scoped.page_repo
    page = await repo.get_space_page("pg-a", space_id="space-a")
    await repo.save(replace(page, pending_base_seq=0), space_id="space-a")
    assert await repo.list_pending_drafts() == [("space-a", "pg-a")]
    await repo.delete("pg-a", space_id="space-a")
    assert await repo.list_pending_drafts() == []


async def test_a_stub_tombstone_is_insert_only(scoped):
    repo = scoped.page_repo
    assert await repo.tombstone(
        "pg-new", space_id="space-a", created_by="u-c", deleted_by="u-d"
    )
    assert await repo.is_page_deleted("pg-new", space_id="space-a")
    assert await repo.get_space_page("pg-new", space_id="space-a") is None
    # A held id (live or tombstoned, any space) is never touched.
    assert not await repo.tombstone("pg-b", space_id="space-a", created_by="u-c")
    assert (await repo.get_space_page("pg-b", space_id="space-b")) is not None
    assert not await repo.tombstone("pg-new", space_id="space-a", created_by="x")


async def test_page_tombstones_list_newest_first_and_since(scoped):
    repo = scoped.page_repo
    await repo.delete("pg-a", space_id="space-a", deleted_by="u-d")
    await scoped.db.enqueue(
        "UPDATE space_pages SET deleted_at='2020-01-01 00:00:00' WHERE id='pg-a'"
    )
    await repo.tombstone("pg-x", space_id="space-a", created_by="u-c")
    got = await repo.list_page_tombstones("space-a")
    assert [(t.id, t.created_by, t.deleted_by) for t in got] == [
        ("pg-x", "u-c", ""),
        ("pg-a", "uid-owner", "u-d"),
    ]
    since = await repo.list_page_tombstones(
        "space-a", since="2021-01-01T00:00:00+00:00"
    )
    assert [t.id for t in since] == ["pg-x"]
    assert [t.id for t in await repo.list_page_tombstones("space-a", limit=1)] == [
        "pg-x"
    ]
    assert await repo.list_page_tombstones("space-b") == []


async def test_raise_seq_only_raises_a_live_pages_seq(scoped):
    repo = scoped.page_repo
    assert await repo.raise_seq("pg-a", space_id="space-a", seq=7) is True
    assert (await repo.get_space_page("pg-a", space_id="space-a")).seq == 7
    assert await repo.raise_seq("pg-a", space_id="space-a", seq=5) is False
    assert (await repo.get_space_page("pg-a", space_id="space-a")).seq == 7
    assert await repo.raise_seq("pg-a", space_id="space-b", seq=9) is False
    await repo.delete("pg-a", space_id="space-a")
    assert await repo.raise_seq("pg-a", space_id="space-a", seq=9) is False


async def test_an_unconfirmed_tombstone_revives_for_the_hosts_seq(scoped):
    from dataclasses import replace

    repo = scoped.page_repo
    page = await repo.get_space_page("pg-a", space_id="space-a")
    await repo.save(replace(page, seq=3), space_id="space-a")
    await repo.delete("pg-a", space_id="space-a", confirmed=False)
    assert not await repo.revive("pg-a", space_id="space-a", seq=2)  # older
    assert await repo.revive("pg-a", space_id="space-a", seq=3)
    back = await repo.get_space_page("pg-a", space_id="space-a")
    assert (back.seq, back.title, back.created_by) == (0, "", "uid-owner")
    assert not await repo.is_page_deleted("pg-a", space_id="space-a")


async def test_a_confirmed_tombstone_never_revives(scoped):
    repo = scoped.page_repo
    await repo.delete("pg-a", space_id="space-a", confirmed=False)
    assert await repo.confirm_delete("pg-a", space_id="space-a")
    assert not await repo.confirm_delete("pg-a", space_id="space-a")
    assert not await repo.revive("pg-a", space_id="space-a", seq=99)
    # Host-made deletes and stubs are confirmed from the start.
    await repo.delete("pg-b", space_id="space-b")
    assert not await repo.revive("pg-b", space_id="space-b", seq=99)
    await repo.tombstone("pg-s", space_id="space-a", created_by="u")
    assert not await repo.revive("pg-s", space_id="space-a", seq=99)
