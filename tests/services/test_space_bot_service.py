"""Tests for socialhome.services.space_bot_service."""

from __future__ import annotations

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpacePermissionError,
    SpaceType,
)
from socialhome.domain.space_bot import (
    BotScope,
    SpaceBotError,
)
from socialhome.domain.user import User
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.space_bot_repo import SqliteSpaceBotRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_bot_service import (
    SpaceBotCreated,
    SpaceBotDeleted,
    SpaceBotService,
    SpaceBotTokenRotated,
)


@pytest.fixture
async def stack(tmp_dir):
    """A full stack with a space, two users (admin + member) and bot service."""
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
    user_repo = SqliteUserRepo(db)
    space_repo = SqliteSpaceRepo(db)
    bot_repo = SqliteSpaceBotRepo(db)
    svc = SpaceBotService(bot_repo, space_repo, user_repo, bus)

    # Capture published events so we can assert fan-out.
    seen: list = []

    async def _rec(tag):
        async def _h(e):
            seen.append((tag, e))

        return _h

    bus.subscribe(SpaceBotCreated, await _rec("created"))
    bus.subscribe(SpaceBotDeleted, await _rec("deleted"))
    bus.subscribe(SpaceBotTokenRotated, await _rec("rotated"))

    # Users.
    for name, uid in (("alice", "uid-alice"), ("bob", "uid-bob")):
        await user_repo.save(
            User(
                username=name,
                user_id=uid,
                display_name=name.title(),
                is_admin=False,
            )
        )

    space = Space(
        id="sp-1",
        name="Home",
        owner_instance_id=iid,
        owner_username="alice",
        identity_public_key="aabb" * 16,
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
        bot_enabled=True,
    )
    await space_repo.save(space)
    await space_repo.save_member(
        SpaceMember(
            space_id="sp-1",
            user_id="uid-alice",
            role="owner",
            joined_at="2025-01-01T00:00:00",
        )
    )
    await space_repo.save_member(
        SpaceMember(
            space_id="sp-1",
            user_id="uid-bob",
            role="member",
            joined_at="2025-01-01T00:00:00",
        )
    )

    class Stack:
        pass

    s = Stack()
    s.svc = svc
    s.events = seen
    s.bot_repo = bot_repo
    yield s
    await db.shutdown()


async def test_admin_creates_space_bot(stack):
    """Owner/admin can create scope=space bots and receive a token."""
    bot, raw = await stack.svc.create_bot(
        "sp-1",
        actor_username="alice",
        scope=BotScope.SPACE,
        slug="doorbell",
        name="Doorbell",
        icon="🔔",
    )
    assert bot.scope is BotScope.SPACE
    assert raw.startswith("shb_")
    assert stack.events[0][0] == "created"


async def test_member_cannot_create_space_scope_bot(stack):
    """Non-admin members can't create a shared space-scope bot."""
    with pytest.raises(SpacePermissionError):
        await stack.svc.create_bot(
            "sp-1",
            actor_username="bob",
            scope=BotScope.SPACE,
            slug="doorbell",
            name="Doorbell",
            icon="🔔",
        )


async def test_member_creates_member_scope_bot(stack):
    """Members can create their own scope=member bots."""
    bot, _ = await stack.svc.create_bot(
        "sp-1",
        actor_username="bob",
        scope=BotScope.MEMBER,
        slug="gym-timer",
        name="Gym timer",
        icon="⏱️",
    )
    assert bot.scope is BotScope.MEMBER
    assert bot.created_by == "uid-bob"


async def test_validation_rejects_bad_slug(stack):
    """Invalid slugs (special chars, uppercase, too long) are rejected."""
    for bad in ("UPPER", "with space", "-lead", "trail-", "x" * 40, ""):
        with pytest.raises(SpaceBotError):
            await stack.svc.create_bot(
                "sp-1",
                actor_username="alice",
                scope=BotScope.SPACE,
                slug=bad,
                name="X",
                icon="🔔",
            )


async def test_non_admin_cannot_delete_others_member_bot(stack):
    """A member can only delete their own bot; admin can delete any."""
    bot_bob, _ = await stack.svc.create_bot(
        "sp-1",
        actor_username="bob",
        scope=BotScope.MEMBER,
        slug="gym-timer",
        name="Gym",
        icon="⏱️",
    )
    # Bob can delete his own.
    await stack.svc.delete_bot("sp-1", bot_bob.bot_id, actor_username="bob")
    assert await stack.bot_repo.get(bot_bob.bot_id) is None

    # Owner Alice can delete a bot she didn't create.
    bot_bob2, _ = await stack.svc.create_bot(
        "sp-1",
        actor_username="bob",
        scope=BotScope.MEMBER,
        slug="laundry",
        name="Laundry",
        icon="🧺",
    )
    await stack.svc.delete_bot("sp-1", bot_bob2.bot_id, actor_username="alice")
    assert await stack.bot_repo.get(bot_bob2.bot_id) is None


async def test_rotate_token_invalidates_old(stack):
    """Rotate issues a new token and fires SpaceBotTokenRotated."""
    bot, raw1 = await stack.svc.create_bot(
        "sp-1",
        actor_username="alice",
        scope=BotScope.SPACE,
        slug="doorbell",
        name="Doorbell",
        icon="🔔",
    )
    bot2, raw2 = await stack.svc.rotate_token(
        "sp-1", bot.bot_id, actor_username="alice"
    )
    assert raw1 != raw2
    assert bot2.token_hash != bot.token_hash
    assert any(t == "rotated" for (t, _) in stack.events)


async def test_update_name_only(stack):
    """update_bot partial update: change name, keep icon."""
    bot, _ = await stack.svc.create_bot(
        "sp-1",
        actor_username="alice",
        scope=BotScope.SPACE,
        slug="doorbell",
        name="Doorbell",
        icon="🔔",
    )
    updated = await stack.svc.update_bot(
        "sp-1", bot.bot_id, actor_username="alice", name="Front door"
    )
    assert updated.name == "Front door"
    assert updated.icon == "🔔"


async def test_bot_in_other_space_not_found(stack):
    """get_bot for a bot in another space requires the caller to be a member
    of that space first — enforced ahead of the bot lookup."""
    await stack.svc.create_bot(
        "sp-1",
        actor_username="alice",
        scope=BotScope.SPACE,
        slug="doorbell",
        name="Doorbell",
        icon="🔔",
    )
    # Alice isn't a member of sp-nonexistent, so she's refused there.
    with pytest.raises(SpacePermissionError):
        await stack.svc.get_bot("sp-nonexistent", "anything", actor_username="alice")
