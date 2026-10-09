"""Tests for SqliteStickyRepo — sticky notes (household and space-scoped)."""

from __future__ import annotations

import pytest

from socialhome.federation.owner_bound_id import (
    SPACE_STICKY_KIND,
    OwnerBinding,
    check_owner_bound_id,
    is_owner_bound,
)
from socialhome.repositories.sticky_repo import SqliteStickyRepo, DEFAULT_COLOR


@pytest.fixture
async def env(tmp_dir):
    """Env with a sticky repo over a real SQLite database."""
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

    class E:
        pass

    e = E()
    e.db = db
    e.repo = SqliteStickyRepo(db)
    yield e
    await db.shutdown()


async def test_add_and_get_sticky(env):
    """add creates a sticky note; get retrieves it by id."""
    sticky = await env.repo.add(author="uid-alice", content="Remember this!")
    assert sticky.content == "Remember this!"
    fetched = await env.repo.get(sticky.id)
    assert fetched is not None
    assert fetched.content == "Remember this!"


async def test_add_empty_content_raises(env):
    """add raises ValueError when content is empty or whitespace."""
    with pytest.raises(ValueError, match="must not be empty"):
        await env.repo.add(author="uid-alice", content="   ")


async def test_get_missing_sticky_returns_none(env):
    """get returns None for an unknown sticky id."""
    assert await env.repo.get("no-such-sticky") is None


async def test_add_uses_default_color(env):
    """add uses DEFAULT_COLOR when no color is specified."""
    sticky = await env.repo.add(author="uid-alice", content="Default color")
    assert sticky.color == DEFAULT_COLOR


async def test_add_with_custom_color(env):
    """add stores the specified color."""
    sticky = await env.repo.add(author="uid-alice", content="Colored", color="#FF0000")
    assert sticky.color == "#FF0000"


async def test_list_household_stickies(env):
    """list() with no space_id returns only household-scoped stickies."""
    s1 = await env.repo.add(author="uid-alice", content="HH1")
    s2 = await env.repo.add(author="uid-alice", content="HH2")
    result = await env.repo.list()
    ids = [s.id for s in result]
    assert s1.id in ids
    assert s2.id in ids


async def test_list_space_stickies(env):
    """list(space_id=...) returns only stickies for that space."""
    # Seed a space so the FK constraint is satisfied
    await env.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, identity_public_key)"
        " VALUES(?,?,?,?,?)",
        ("sp-1", "TestSpace", "inst-x", "uid-alice", "aabb" * 16),
    )
    space_sticky = await env.repo.add(
        author="uid-alice", content="Space note", space_id="sp-1"
    )
    household_sticky = await env.repo.add(author="uid-alice", content="HH note")
    space_result = await env.repo.list(space_id="sp-1")
    hh_result = await env.repo.list()
    space_ids = [s.id for s in space_result]
    hh_ids = [s.id for s in hh_result]
    assert space_sticky.id in space_ids
    assert space_sticky.id not in hh_ids
    assert household_sticky.id in hh_ids


async def test_update_content(env):
    """update_content changes the sticky's text."""
    sticky = await env.repo.add(author="uid-alice", content="Old")
    await env.repo.update_content(sticky.id, "New content", space_id=None)
    fetched = await env.repo.get(sticky.id)
    assert fetched.content == "New content"


async def test_update_content_empty_raises(env):
    """update_content raises ValueError when new content is empty."""
    sticky = await env.repo.add(author="uid-alice", content="Valid")
    with pytest.raises(ValueError):
        await env.repo.update_content(sticky.id, "", space_id=None)


async def test_update_position(env):
    """update_position changes x and y coordinates."""
    sticky = await env.repo.add(author="uid-alice", content="Move me")
    await env.repo.update_position(sticky.id, 100.5, 200.75, space_id=None)
    fetched = await env.repo.get(sticky.id)
    assert abs(fetched.position_x - 100.5) < 0.01
    assert abs(fetched.position_y - 200.75) < 0.01


async def test_update_color(env):
    """update_color changes the sticky's color."""
    sticky = await env.repo.add(author="uid-alice", content="Recolor")
    await env.repo.update_color(sticky.id, "#123456", space_id=None)
    fetched = await env.repo.get(sticky.id)
    assert fetched.color == "#123456"


async def test_delete_sticky(env):
    """delete removes the sticky note."""
    sticky = await env.repo.add(author="uid-alice", content="Delete me")
    await env.repo.delete(sticky.id, space_id=None)
    assert await env.repo.get(sticky.id) is None


# ─── §24.11 cross-space scoping ──────────────────────────────


async def _make_space(db, space_id):
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?,?,?,?,?)",
        (space_id, space_id, "inst-1", "owner", "ab" * 32),
    )


@pytest.fixture
async def two_spaces(env):
    """Two space stickies (space-a / space-b) plus one household sticky."""
    from socialhome.domain.sticky import Sticky

    await _make_space(env.db, "space-a")
    await _make_space(env.db, "space-b")
    for sid, rid in (("space-a", "st-a"), ("space-b", "st-b"), (None, "st-hh")):
        await env.repo.save(
            Sticky(
                id=rid,
                author="uid-owner",
                content=f"body-{rid}",
                color="#FFF9B1",
                position_x=1.0,
                position_y=2.0,
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
                space_id=sid,
            ),
            space_id=sid,
        )
    return env


async def _snapshot(env, sticky_id):
    row = await env.db.fetchone("SELECT * FROM stickies WHERE id=?", (sticky_id,))
    return dict(row) if row is not None else None


async def test_update_content_refuses_foreign_space(two_spaces):
    """A writer gated on space A cannot edit space B's sticky."""
    before = await _snapshot(two_spaces, "st-b")
    assert (
        await two_spaces.repo.update_content("st-b", "hax", space_id="space-a") is False
    )
    assert await _snapshot(two_spaces, "st-b") == before


async def test_update_content_own_space_still_works(two_spaces):
    """The same call scoped to the sticky's own space applies."""
    assert (
        await two_spaces.repo.update_content("st-b", "edited", space_id="space-b")
        is True
    )
    assert (await two_spaces.repo.get("st-b")).content == "edited"


async def test_update_position_and_color_are_space_scoped(two_spaces):
    """Position and colour updates honour the gated space too."""
    before = await _snapshot(two_spaces, "st-b")
    assert (
        await two_spaces.repo.update_position("st-b", 9.0, 9.0, space_id="space-a")
        is False
    )
    assert (
        await two_spaces.repo.update_color("st-b", "#000000", space_id="space-a")
        is False
    )
    assert await _snapshot(two_spaces, "st-b") == before
    assert (
        await two_spaces.repo.update_position("st-b", 9.0, 9.0, space_id="space-b")
        is True
    )
    assert (
        await two_spaces.repo.update_color("st-b", "#000000", space_id="space-b")
        is True
    )


async def test_delete_refuses_foreign_space(two_spaces):
    """A delete routed as space A cannot remove space B's sticky."""
    assert await two_spaces.repo.delete("st-b", space_id="space-a") is False
    assert await two_spaces.repo.get("st-b") is not None
    assert await two_spaces.repo.delete("st-b", space_id="space-b") is True
    assert await two_spaces.repo.get("st-b") is None


async def test_space_scoped_call_cannot_touch_household_sticky(two_spaces):
    """``space_id IS ?`` is null-safe: a space write misses household rows."""
    before = await _snapshot(two_spaces, "st-hh")
    assert (
        await two_spaces.repo.update_content("st-hh", "hax", space_id="space-a")
        is False
    )
    assert await two_spaces.repo.delete("st-hh", space_id="space-a") is False
    assert await _snapshot(two_spaces, "st-hh") == before
    assert await two_spaces.repo.update_content("st-hh", "mine", space_id=None) is True


async def test_save_upsert_refuses_cross_space_id(two_spaces):
    """Re-saving space B's id under space A leaves B untouched and adds no row."""
    from socialhome.domain.sticky import Sticky

    before = await _snapshot(two_spaces, "st-b")
    stolen = Sticky(
        id="st-b",
        author="uid-attacker",
        content="stolen",
        color="#FF0000",
        position_x=0.0,
        position_y=0.0,
        created_at="2026-02-01T00:00:00+00:00",
        updated_at="2026-02-01T00:00:00+00:00",
        space_id="space-a",
    )
    assert await two_spaces.repo.save(stolen, space_id="space-a") is False
    assert await _snapshot(two_spaces, "st-b") == before
    assert [s.id for s in await two_spaces.repo.list(space_id="space-a")] == ["st-a"]


async def test_a_space_sticky_id_commits_to_its_author(env):
    """v_36: a space sticky federates, so its id is owner-bound to its
    author in its space; a household sticky keeps a plain id."""
    await _make_space(env.db, "sp-1")
    sticky = await env.repo.add(author="uid-alice", content="x", space_id="sp-1")
    assert (
        check_owner_bound_id(
            SPACE_STICKY_KIND, sticky.id, space_id="sp-1", owner_user_id="uid-alice"
        )
        is OwnerBinding.VALID
    )
    assert (
        check_owner_bound_id(
            SPACE_STICKY_KIND, sticky.id, space_id="sp-1", owner_user_id="uid-bob"
        )
        is OwnerBinding.MISMATCH
    )
    home = await env.repo.add(author="uid-alice", content="y")
    assert not is_owner_bound(home.id)


async def test_get_scoped_only_returns_rows_in_scope(env):
    """``get_scoped`` is the defence-in-depth read: a household lookup
    never sees a space row and a space lookup never sees another
    space's (or a household) row."""
    await env.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-1', 'S', 'inst', 'admin', ?)",
        ("ab" * 32,),
    )
    home = await env.repo.add(author="u1", content="home")
    space = await env.repo.add(author="u1", content="space", space_id="sp-1")

    assert (await env.repo.get_scoped(home.id, space_id=None)).content == "home"
    assert await env.repo.get_scoped(home.id, space_id="sp-1") is None
    assert (await env.repo.get_scoped(space.id, space_id="sp-1")).content == "space"
    assert await env.repo.get_scoped(space.id, space_id=None) is None
    assert await env.repo.get_scoped(space.id, space_id="sp-2") is None
    assert await env.repo.get_scoped("missing", space_id=None) is None


async def test_read_never_returns_a_legacy_non_hex_color(env):
    """Rows stored before colours were validated (a peer's ``url(...)``,
    the old inbound default ``"yellow"``) read back as the default."""
    await env.db.enqueue(
        "INSERT INTO stickies(id, space_id, author, content, color)"
        " VALUES('legacy', NULL, 'u1', 'x', 'url(https://evil.example/t.png)')",
    )
    s = await env.repo.get("legacy")
    assert s is not None and s.color == DEFAULT_COLOR
    assert [x.color for x in await env.repo.list(space_id=None)] == [DEFAULT_COLOR]


# ─── Tombstones (migration 0085, §25.6 ``stickies_deleted``) ─────────


async def test_a_space_delete_keeps_a_content_free_tombstone(two_spaces):
    env = two_spaces
    assert await env.repo.delete("st-a", space_id="space-a", deleted_by="u-del")
    row = await _snapshot(env, "st-a")
    assert row is not None and row["deleted_at"] and row["deleted_by"] == "u-del"
    assert row["content"] == ""
    # Every read treats it as gone …
    assert await env.repo.get("st-a") is None
    assert await env.repo.get_scoped("st-a", space_id="space-a") is None
    assert [s.id for s in await env.repo.list(space_id="space-a")] == []
    assert await env.repo.list_since("space-a", "1970-01-01T00:00:00+00:00") == []
    assert await env.repo.is_deleted("st-a", space_id="space-a")
    assert not await env.repo.is_deleted("st-a", space_id="space-b")
    # … a second delete finds nothing …
    assert not await env.repo.delete("st-a", space_id="space-a")
    # … and no write brings it back.
    assert not await env.repo.update_content("st-a", "back", space_id="space-a")
    assert not await env.repo.update_position("st-a", 1, 1, space_id="space-a")
    assert not await env.repo.update_color("st-a", "#000000", space_id="space-a")
    from socialhome.domain.sticky import Sticky

    revived = Sticky(
        id="st-a",
        author="uid-owner",
        content="back",
        color="#FFF9B1",
        position_x=0.0,
        position_y=0.0,
        created_at="",
        updated_at="",
        space_id="space-a",
    )
    assert not await env.repo.save(revived, space_id="space-a")
    assert (await _snapshot(env, "st-a"))["content"] == ""


async def test_a_household_delete_still_removes_the_row(two_spaces):
    env = two_spaces
    assert await env.repo.delete("st-hh", space_id=None, deleted_by="u")
    assert await _snapshot(env, "st-hh") is None


async def test_tombstones_page_per_space_by_keyset(two_spaces):
    env = two_spaces
    ids = []
    for i in range(5):
        s = await env.repo.add(author="u-a", content=f"n{i}", space_id="space-a")
        ids.append(s.id)
        await env.repo.delete(s.id, space_id="space-a", deleted_by="u-a")
    await env.repo.delete("st-b", space_id="space-b")
    page, cursor = await env.repo.list_tombstones_page("space-a", limit=3)
    assert [t.id for t in page] == ids[:3] and cursor is not None
    rest, end = await env.repo.list_tombstones_page("space-a", cursor=cursor, limit=3)
    assert [t.id for t in rest] == ids[3:] and end is None
    first = page[0]
    assert (first.owner, first.deleted_by) == ("u-a", "u-a")
    assert first.created_at and first.deleted_at


async def test_a_stub_tombstone_is_insert_only(two_spaces):
    env = two_spaces
    assert await env.repo.tombstone(
        "st-new", space_id="space-a", author="u-a", created_at="c", deleted_by="u-m"
    )
    assert await env.repo.is_deleted("st-new", space_id="space-a")
    # An id held already (live, or in another space) is never touched.
    assert not await env.repo.tombstone("st-b", space_id="space-a", author="u-a")
    assert (await _snapshot(env, "st-b"))["deleted_at"] is None


async def test_tombstones_page_since_a_stamp(two_spaces):
    env = two_spaces
    first = await env.repo.add(author="u-a", content="one", space_id="space-a")
    second = await env.repo.add(author="u-a", content="two", space_id="space-a")
    await env.repo.delete(first.id, space_id="space-a", deleted_by="u-a")
    row = await env.db.fetchone("SELECT seq FROM sync_seq_counter WHERE id=1")
    mark = int(row["seq"])
    assert await env.repo.list_tombstones_page("space-a", since=mark) == ([], None)
    await env.repo.delete(second.id, space_id="space-a", deleted_by="u-a")
    page, _ = await env.repo.list_tombstones_page("space-a", since=mark)
    assert [t.id for t in page] == [second.id]
