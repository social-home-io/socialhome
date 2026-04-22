"""Tests for socialhome.services.bot_bridge_service."""

from __future__ import annotations

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.conversation import Conversation, ConversationType
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceType,
)
from socialhome.domain.space_bot import BotScope, SpaceBotDisabledError
from socialhome.domain.user import SYSTEM_AUTHOR
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.conversation_repo import SqliteConversationRepo
from socialhome.repositories.space_bot_repo import SqliteSpaceBotRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.bot_bridge_service import (
    BotBridgeInvalidError,
    BotBridgeService,
    MAX_MESSAGE_LEN,
)


@pytest.fixture
async def stack(tmp_dir):
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
    space_repo = SqliteSpaceRepo(db)
    space_post_repo = SqliteSpacePostRepo(db)
    conv_repo = SqliteConversationRepo(db)
    bot_repo = SqliteSpaceBotRepo(db)
    svc = BotBridgeService(space_post_repo, space_repo, conv_repo, bus)

    await space_repo.save(
        Space(
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
    )
    bot, _raw = await bot_repo.create(
        bot_id="b1",
        space_id="sp-1",
        scope=BotScope.SPACE,
        slug="doorbell",
        name="Doorbell",
        icon="🔔",
        created_by="uid-alice",
    )

    class Stack:
        pass

    from datetime import datetime, timezone

    s = Stack()
    s.db = db
    s.svc = svc
    s.bot = bot
    s.space_repo = space_repo
    s.space_post_repo = space_post_repo
    s.conv_repo = conv_repo
    s.iso_now = datetime.now(timezone.utc)
    yield s
    await db.shutdown()


async def test_notify_space_persists_post_with_bot_id(stack):
    """The stored post has SYSTEM_AUTHOR + bot_id set."""
    post = await stack.svc.notify_space(stack.bot, title="Ring", message="Front door")
    assert post.author == SYSTEM_AUTHOR
    assert post.bot_id == "b1"
    # Round-trip through the repo to confirm bot_id persists.
    _, reloaded = await stack.space_post_repo.get(post.id)
    assert reloaded.bot_id == "b1"
    assert "**Ring**" in (reloaded.content or "")


async def test_notify_space_respects_kill_switch(stack):
    """When bot_enabled=False, posting is blocked even with a valid bot."""
    space = await stack.space_repo.get("sp-1")
    # Immutable dataclass: save a new one with bot_enabled=False.
    import copy

    await stack.space_repo.save(copy.replace(space, bot_enabled=False))
    with pytest.raises(SpaceBotDisabledError):
        await stack.svc.notify_space(stack.bot, title=None, message="Blocked")


async def test_payload_validation(stack):
    """Empty message / too-long message raise BotBridgeInvalidError."""
    with pytest.raises(BotBridgeInvalidError):
        await stack.svc.notify_space(stack.bot, title=None, message="")
    with pytest.raises(BotBridgeInvalidError):
        await stack.svc.notify_space(
            stack.bot, title=None, message="x" * (MAX_MESSAGE_LEN + 1)
        )


async def test_notify_conversation(stack):
    """DM bot posts land with SYSTEM_AUTHOR as sender and rely on bot_enabled."""
    from datetime import datetime, timezone

    conv = Conversation(
        id="c1",
        type=ConversationType.DM,
        created_at=datetime.now(timezone.utc),
        bot_enabled=True,
    )
    await stack.conv_repo.create(conv)
    msg = await stack.svc.notify_conversation(
        conversation_id="c1",
        sender_user_id="uid-alice",
        recipient_user_ids=("bob",),
        title="Laundry",
        message="Done",
    )
    assert msg.sender_user_id == SYSTEM_AUTHOR
    # Kill-switch path — a separate conversation with bot_enabled=False.
    conv_off = Conversation(
        id="c2",
        type=ConversationType.DM,
        created_at=datetime.now(timezone.utc),
        bot_enabled=False,
    )
    await stack.conv_repo.create(conv_off)
    with pytest.raises(SpaceBotDisabledError):
        await stack.svc.notify_conversation(
            conversation_id="c2",
            sender_user_id="uid-alice",
            recipient_user_ids=("bob",),
            title=None,
            message="Blocked",
        )
