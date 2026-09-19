"""Tests for SqliteStickyRepo — sticky notes (household and space-scoped)."""

from __future__ import annotations

import pytest

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


# ─── §24.11 cross-space scoping (issue #693) ──────────────────────────────


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
