"""Tests for SqliteSpaceRepo — spaces, members, instances, bans, invites, etc."""

from __future__ import annotations

import asyncio

import pytest

from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceType,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo


@pytest.fixture
async def env(tmp_dir):
    """Env with a space repo and a seeded user."""
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
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("alice", "uid-alice", "Alice"),
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("bob", "uid-bob", "Bob"),
    )

    class E:
        pass

    e = E()
    e.db = db
    e.kp = kp
    e.iid = iid
    e.repo = SqliteSpaceRepo(db)
    yield e
    await db.shutdown()


def _space(
    space_id: str = "sp-1",
    name: str = "TestSpace",
    space_type: SpaceType = SpaceType.PRIVATE,
) -> Space:
    return Space(
        id=space_id,
        name=name,
        owner_instance_id="inst-x",
        owner_username="alice",
        identity_public_key="aabb" * 16,
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=space_type,
        join_mode=JoinMode.INVITE_ONLY,
    )


def _member(
    space_id: str, user_id: str = "uid-alice", role: str = "member"
) -> SpaceMember:
    return SpaceMember(
        space_id=space_id,
        user_id=user_id,
        role=role,
        joined_at="2025-01-01T00:00:00",
    )


# ── Spaces ─────────────────────────────────────────────────────────────────


async def test_save_and_get_space(env):
    """save persists a space; get retrieves it."""
    space = _space("sp-1")
    await env.repo.save(space)
    fetched = await env.repo.get("sp-1")
    assert fetched is not None
    assert fetched.name == "TestSpace"


async def test_get_missing_space(env):
    """get returns None for an unknown space id."""
    assert await env.repo.get("nope") is None


async def test_list_by_type(env):
    """list_by_type returns non-dissolved spaces matching the given type."""
    await env.repo.save(_space("sp-priv1", space_type=SpaceType.PRIVATE))
    await env.repo.save(_space("sp-priv2", name="Other", space_type=SpaceType.PRIVATE))
    results = await env.repo.list_by_type(SpaceType.PRIVATE)
    ids = [s.id for s in results]
    assert "sp-priv1" in ids
    assert "sp-priv2" in ids


async def test_list_by_type_excludes_dissolved(env):
    """list_by_type does not return dissolved spaces."""
    await env.repo.save(_space("sp-dis"))
    await env.repo.mark_dissolved("sp-dis")
    results = await env.repo.list_by_type(SpaceType.PRIVATE)
    assert not any(s.id == "sp-dis" for s in results)


async def test_mark_dissolved(env):
    """mark_dissolved sets dissolved=True on the space."""
    await env.repo.save(_space("sp-md"))
    await env.repo.mark_dissolved("sp-md")
    fetched = await env.repo.get("sp-md")
    assert fetched.dissolved is True


async def test_increment_config_sequence_atomic(env):
    """increment_config_sequence returns a strictly increasing sequence."""
    await env.repo.save(_space("sp-seq"))
    v1 = await env.repo.increment_config_sequence("sp-seq")
    v2 = await env.repo.increment_config_sequence("sp-seq")
    assert v1 == 1
    assert v2 == 2


async def test_increment_config_sequence_concurrent(env):
    """Concurrent increments each return a unique sequence number."""
    await env.repo.save(_space("sp-conc"))
    results = await asyncio.gather(
        env.repo.increment_config_sequence("sp-conc"),
        env.repo.increment_config_sequence("sp-conc"),
        env.repo.increment_config_sequence("sp-conc"),
    )
    assert sorted(results) == [1, 2, 3]


# ── Members ────────────────────────────────────────────────────────────────


async def test_save_and_get_member(env):
    """save_member persists; get_member retrieves a single member row."""
    await env.repo.save(_space("sp-mem"))
    member = _member("sp-mem", "uid-alice", role="owner")
    await env.repo.save_member(member)
    fetched = await env.repo.get_member("sp-mem", "uid-alice")
    assert fetched is not None
    assert fetched.role == "owner"


async def test_list_members(env):
    """list_members returns all members of a space."""
    await env.repo.save(_space("sp-lm"))
    await env.repo.save_member(_member("sp-lm", "uid-alice", role="owner"))
    await env.repo.save_member(_member("sp-lm", "uid-bob", role="member"))
    members = await env.repo.list_members("sp-lm")
    user_ids = {m.user_id for m in members}
    assert user_ids == {"uid-alice", "uid-bob"}


async def test_delete_member(env):
    """delete_member removes the member row."""
    await env.repo.save(_space("sp-dm"))
    await env.repo.save_member(_member("sp-dm", "uid-bob"))
    await env.repo.delete_member("sp-dm", "uid-bob")
    assert await env.repo.get_member("sp-dm", "uid-bob") is None


async def test_set_role(env):
    """set_role updates a member's role."""
    await env.repo.save(_space("sp-role"))
    await env.repo.save_member(_member("sp-role", "uid-alice", role="member"))
    await env.repo.set_role("sp-role", "uid-alice", "admin")
    fetched = await env.repo.get_member("sp-role", "uid-alice")
    assert fetched.role == "admin"


async def test_set_role_invalid_raises(env):
    """set_role raises ValueError for an unknown role string."""
    await env.repo.save(_space("sp-bad-role"))
    await env.repo.save_member(_member("sp-bad-role", "uid-alice"))
    with pytest.raises(ValueError, match="invalid role"):
        await env.repo.set_role("sp-bad-role", "uid-alice", "superuser")


# ── Space instances ────────────────────────────────────────────────────────


async def test_add_and_list_space_instances(env):
    """add_space_instance adds an instance link; list_member_instances lists them."""
    await env.repo.save(_space("sp-inst"))
    await env.repo.add_space_instance("sp-inst", "inst-remote-1")
    await env.repo.add_space_instance("sp-inst", "inst-remote-2")
    instances = await env.repo.list_member_instances("sp-inst")
    assert set(instances) == {"inst-remote-1", "inst-remote-2"}


# ── Bans ───────────────────────────────────────────────────────────────────


async def test_ban_and_is_banned(env):
    """ban_member bans a user; is_banned returns True."""
    await env.repo.save(_space("sp-ban"))
    await env.repo.save_member(_member("sp-ban", "uid-bob"))
    await env.repo.ban_member("sp-ban", "uid-bob", "uid-alice")
    assert await env.repo.is_banned("sp-ban", "uid-bob") is True


async def test_unban_member(env):
    """unban_member removes the ban."""
    await env.repo.save(_space("sp-unban"))
    await env.repo.save_member(_member("sp-unban", "uid-bob"))
    await env.repo.ban_member("sp-unban", "uid-bob", "uid-alice")
    await env.repo.unban_member("sp-unban", "uid-bob")
    assert await env.repo.is_banned("sp-unban", "uid-bob") is False


async def test_list_bans(env):
    """list_bans returns the ban records for a space."""
    await env.repo.save(_space("sp-bans"))
    await env.repo.save_member(_member("sp-bans", "uid-bob"))
    await env.repo.ban_member("sp-bans", "uid-bob", "uid-alice", reason="spam")
    bans = await env.repo.list_bans("sp-bans")
    assert len(bans) == 1
    assert bans[0]["user_id"] == "uid-bob"


# ── Invite tokens ──────────────────────────────────────────────────────────


async def test_create_and_consume_invite_token(env):
    """create_invite_token produces a token that can be consumed once."""
    await env.repo.save(_space("sp-tok"))
    token = await env.repo.create_invite_token("sp-tok", "uid-alice", uses=1)
    assert token
    result = await env.repo.consume_invite_token(token)
    assert result is not None
    assert result["space_id"] == "sp-tok"


async def test_consume_exhausted_token_returns_none(env):
    """consume_invite_token returns None after all uses are consumed."""
    await env.repo.save(_space("sp-exhaust"))
    token = await env.repo.create_invite_token("sp-exhaust", "uid-alice", uses=1)
    await env.repo.consume_invite_token(token)
    result = await env.repo.consume_invite_token(token)
    assert result is None


async def test_consume_missing_token_returns_none(env):
    """consume_invite_token returns None for a non-existent token."""
    result = await env.repo.consume_invite_token("no-such-token")
    assert result is None


# ── Invitations ────────────────────────────────────────────────────────────


async def test_save_and_get_invitation(env):
    """save_invitation creates an invitation; get_invitation retrieves it."""
    await env.repo.save(_space("sp-inv"))
    inv_id = await env.repo.save_invitation(
        "sp-inv",
        "uid-bob",
        "uid-alice",
    )
    inv = await env.repo.get_invitation(inv_id)
    assert inv is not None
    assert inv["invited_user_id"] == "uid-bob"


async def test_update_invitation_status(env):
    """update_invitation_status changes the invitation's status field."""
    await env.repo.save(_space("sp-invst"))
    inv_id = await env.repo.save_invitation("sp-invst", "uid-bob", "uid-alice")
    await env.repo.update_invitation_status(inv_id, "accepted")
    inv = await env.repo.get_invitation(inv_id)
    assert inv["status"] == "accepted"


# ── Sidebar pins ───────────────────────────────────────────────────────────


async def test_pin_and_unpin_sidebar(env):
    """pin_sidebar adds; unpin_sidebar removes a pinned space."""
    await env.repo.save(_space("sp-pin"))
    await env.repo.pin_sidebar("uid-alice", "sp-pin", 0)
    # Verify it was inserted (no error)
    await env.repo.unpin_sidebar("uid-alice", "sp-pin")
    # Should not raise


# ── Aliases ────────────────────────────────────────────────────────────────


async def test_set_and_get_space_alias(env):
    """set_space_alias stores a personal alias; get_space_alias retrieves it."""
    await env.repo.save(_space("sp-alias"))
    await env.repo.set_space_alias("sp-alias", "alice", "Family Space")
    alias = await env.repo.get_space_alias("sp-alias", "alice")
    assert alias == "Family Space"


async def test_get_missing_alias_returns_none(env):
    """get_space_alias returns None when no alias is set."""
    await env.repo.save(_space("sp-noalias"))
    alias = await env.repo.get_space_alias("sp-noalias", "alice")
    assert alias is None
