"""Tests for socialhome.services.sticky_service."""

from __future__ import annotations

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import StickyCreated, StickyDeleted, StickyUpdated
from socialhome.domain.space import SpacePermissionError
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.sticky_repo import SqliteStickyRepo
from socialhome.services.sticky_service import StickyService


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    for sid in ("sp-a", "sp-b"):
        await db.enqueue(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key) VALUES(?, ?, 'inst', 'admin', ?)",
            (sid, sid, "ab" * 32),
        )
    for sid, uid, role in (
        ("sp-a", "alice", "owner"),
        ("sp-a", "sub", "subscriber"),
        ("sp-a", "mem", "member"),
        ("sp-b", "alice", "owner"),
    ):
        await db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
            (sid, uid, role),
        )
    bus = EventBus()
    events: list = []

    async def _record(event) -> None:
        events.append(event)

    for et in (StickyCreated, StickyUpdated, StickyDeleted):
        bus.subscribe(et, _record)

    class E:
        pass

    e = E()
    e.db = db
    e.repo = SqliteStickyRepo(db)
    e.svc = StickyService(e.repo, bus, space_repo=SqliteSpaceRepo(db))
    e.events = events
    yield e
    await db.shutdown()


async def test_household_crud_publishes_household_events(env):
    s = await env.svc.create(author="alice", content="milk", space_id=None)
    assert s.space_id is None
    assert [x.id for x in await env.svc.list(space_id=None)] == [s.id]
    up = await env.svc.update(
        s.id,
        space_id=None,
        actor_user_id="alice",
        content="eggs",
        color="#FF0000",
        position_x=4,
    )
    assert (up.content, up.color, up.position_x) == ("eggs", "#FF0000", 4.0)
    await env.svc.delete(s.id, space_id=None, actor_user_id="alice")
    assert await env.repo.get(s.id) is None
    assert [type(e) for e in env.events] == [
        StickyCreated,
        StickyUpdated,
        StickyDeleted,
    ]
    assert all(e.space_id is None for e in env.events)


async def test_household_scope_never_touches_space_sticky(env):
    s = await env.svc.create(author="alice", content="secret", space_id="sp-a")
    env.events.clear()
    with pytest.raises(KeyError):
        await env.svc.update(
            s.id, space_id=None, actor_user_id="alice", content="pwned"
        )
    with pytest.raises(KeyError):
        await env.svc.delete(s.id, space_id=None, actor_user_id="alice")
    assert (await env.repo.get(s.id)).content == "secret"
    assert env.events == []


async def test_space_scope_never_touches_another_space_or_household(env):
    other = await env.svc.create(author="alice", content="b", space_id="sp-b")
    home = await env.svc.create(author="alice", content="h", space_id=None)
    env.events.clear()
    for sid in (other.id, home.id):
        with pytest.raises(KeyError):
            await env.svc.update(
                sid, space_id="sp-a", actor_user_id="alice", content="x"
            )
        with pytest.raises(KeyError):
            await env.svc.delete(sid, space_id="sp-a", actor_user_id="alice")
    assert (await env.repo.get(other.id)).content == "b"
    assert (await env.repo.get(home.id)).content == "h"
    assert env.events == []


async def test_space_update_publishes_space_scoped_event(env):
    s = await env.svc.create(author="alice", content="a", space_id="sp-a")
    await env.svc.update(
        s.id, space_id="sp-a", actor_user_id="alice", position_x=1, position_y=2
    )
    ev = env.events[-1]
    assert isinstance(ev, StickyUpdated)
    assert (ev.space_id, ev.position_x, ev.position_y) == ("sp-a", 1.0, 2.0)


async def test_update_validates_types(env):
    s = await env.svc.create(author="alice", content="a", space_id=None)
    for kwargs in (
        {"content": 5},
        {"content": "  "},
        {"color": ["x"]},
        {"position_x": "nope"},
        {"position_y": {"a": 1}},
    ):
        with pytest.raises(ValueError):
            await env.svc.update(s.id, space_id=None, actor_user_id="alice", **kwargs)
    assert (await env.repo.get(s.id)).content == "a"


async def test_create_validates_types(env):
    with pytest.raises(ValueError):
        await env.svc.create(author="alice", content=None, space_id=None)
    with pytest.raises(ValueError):
        await env.svc.create(
            author="alice", content="ok", space_id=None, position_x="bad"
        )
    with pytest.raises(ValueError):
        await env.svc.create(author="alice", content="ok", space_id=None, color=1)


async def test_require_writer_allows_members(env):
    await env.svc.require_writer("sp-a", "alice")
    await env.svc.require_writer("sp-a", "mem")


async def test_require_writer_rejects_subscriber_and_non_member(env):
    with pytest.raises(SpacePermissionError):
        await env.svc.require_writer("sp-a", "sub")
    with pytest.raises(SpacePermissionError):
        await env.svc.require_writer("sp-b", "mem")


async def test_require_writer_rejects_archived_space(env):
    await env.db.enqueue("UPDATE spaces SET archived=1 WHERE id='sp-a'")
    with pytest.raises(SpacePermissionError):
        await env.svc.require_writer("sp-a", "alice")


async def test_require_writer_unknown_or_dissolved_space_is_keyerror(env):
    with pytest.raises(KeyError):
        await env.svc.require_writer("sp-missing", "alice")
    await env.db.enqueue("UPDATE spaces SET dissolved=1 WHERE id='sp-b'")
    with pytest.raises(KeyError):
        await env.svc.require_writer("sp-b", "alice")


async def test_service_without_bus_is_silent(env):
    svc = StickyService(env.repo, None, space_repo=SqliteSpaceRepo(env.db))
    s = await svc.create(author="alice", content="x", space_id=None)
    await svc.delete(s.id, space_id=None, actor_user_id="alice")
    assert env.events == []


async def test_color_must_be_hex_and_is_canonicalised(env):
    for bad in ("url(https://evil.example/t.png)", "red", "#12345", "x" * 300):
        with pytest.raises(ValueError):
            await env.svc.create(author="alice", content="x", space_id=None, color=bad)
    s = await env.svc.create(author="alice", content="x", space_id=None, color="#abc")
    assert s.color == "#AABBCC"
    with pytest.raises(ValueError):
        await env.svc.update(s.id, space_id=None, actor_user_id="alice", color="url(x)")
    assert (await env.repo.get(s.id)).color == "#AABBCC"


async def test_coordinates_overflow_is_valueerror_and_values_clamp(env):
    with pytest.raises(ValueError):
        await env.svc.create(
            author="alice", content="x", space_id=None, position_x=10**400
        )
    s = await env.svc.create(
        author="alice", content="x", space_id=None, position_x=-1, position_y=1e6
    )
    assert (s.position_x, s.position_y) == (0.0, 700.0)


async def test_content_capped_and_sanitised(env):
    with pytest.raises(ValueError):
        await env.svc.create(author="alice", content="y" * 2001, space_id=None)
    s = await env.svc.create(author="alice", content="a‮b\x00", space_id=None)
    assert s.content == "ab"
    with pytest.raises(ValueError):
        await env.svc.update(
            s.id, space_id=None, actor_user_id="alice", content="z" * 2001
        )


# ─── ADMIN_ONLY stickies (§4.3 feature access levels) ───────────────────


async def _admin_only_board(env):
    for uid, role in (("adm", "admin"), ("mod", "moderator")):
        await env.db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
            ("sp-a", uid, role),
        )
    note = await env.svc.create(author="mem", content="mine", space_id="sp-a")
    await env.db.enqueue(
        "UPDATE spaces SET stickies_access='admin_only' WHERE id='sp-a'"
    )
    env.events.clear()
    return note


@pytest.mark.parametrize("actor", ["mem", "mod"])
@pytest.mark.parametrize(
    "op",
    ["create", "edit_content", "edit_color", "move", "delete"],
)
async def test_admin_only_board_refuses_members_and_moderators(env, actor, op):
    from socialhome.domain.space import AccessAdminOnlyError

    note = await _admin_only_board(env)
    writes = {
        "create": lambda: env.svc.create(author=actor, content="x", space_id="sp-a"),
        "edit_content": lambda: env.svc.update(
            note.id, space_id="sp-a", actor_user_id=actor, content="changed"
        ),
        "edit_color": lambda: env.svc.update(
            note.id, space_id="sp-a", actor_user_id=actor, color="#00FF00"
        ),
        "move": lambda: env.svc.update(
            note.id, space_id="sp-a", actor_user_id=actor, position_x=50
        ),
        "delete": lambda: env.svc.delete(note.id, space_id="sp-a", actor_user_id=actor),
    }
    with pytest.raises(AccessAdminOnlyError):
        await writes[op]()
    held = await env.repo.get(note.id)
    assert held is not None
    assert (held.content, held.position_x) == ("mine", note.position_x)
    assert [s.id for s in await env.svc.list(space_id="sp-a")] == [note.id]
    assert env.events == []


@pytest.mark.parametrize("actor", ["alice", "adm"])
async def test_admin_only_board_lets_admins_write(env, actor):
    note = await _admin_only_board(env)
    created = await env.svc.create(author=actor, content="new", space_id="sp-a")
    moved = await env.svc.update(
        note.id, space_id="sp-a", actor_user_id=actor, position_x=9, content="ok"
    )
    assert (moved.content, moved.position_x) == ("ok", 9.0)
    await env.svc.delete(created.id, space_id="sp-a", actor_user_id=actor)
    assert await env.repo.get(created.id) is None


async def test_the_household_board_has_no_access_level(env):
    """The household board is not a space: nothing to gate."""
    await _admin_only_board(env)
    s = await env.svc.create(author="mem", content="hi", space_id=None)
    await env.svc.update(s.id, space_id=None, actor_user_id="mem", content="yo")
    await env.svc.delete(s.id, space_id=None, actor_user_id="mem")


async def test_space_sticky_events_name_their_actor(env):
    s = await env.svc.create(author="mem", content="a", space_id="sp-a")
    await env.svc.update(s.id, space_id="sp-a", actor_user_id="alice", content="b")
    await env.svc.delete(s.id, space_id="sp-a", actor_user_id="alice")
    assert [e.actor_user_id for e in env.events] == ["mem", "alice", "alice"]
