"""Tests for SqliteConversationRepo — conversations, members, messages, reactions."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.domain.conversation import (
    Conversation,
    ConversationMember,
    ConversationMessage,
    ConversationType,
    RemoteConversationMember,
)
from socialhome.repositories.conversation_repo import SqliteConversationRepo


@pytest.fixture
async def env(tmp_dir):
    """Env with a conversation repo and seeded users."""
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
    e.repo = SqliteConversationRepo(db)
    yield e
    await db.shutdown()


def _conv(
    conv_id: str = "c1", type: ConversationType = ConversationType.DM
) -> Conversation:
    return Conversation(id=conv_id, type=type, created_at=datetime.now(timezone.utc))


def _member(conv_id: str, username: str) -> ConversationMember:
    return ConversationMember(
        conversation_id=conv_id,
        username=username,
        joined_at=datetime.now(timezone.utc).isoformat(),
    )


def _message(
    msg_id: str, conv_id: str, sender: str = "uid-alice", content: str = "Hi"
) -> ConversationMessage:
    return ConversationMessage(
        id=msg_id,
        conversation_id=conv_id,
        sender_user_id=sender,
        content=content,
        created_at=datetime.now(timezone.utc),
    )


# ── Conversations ──────────────────────────────────────────────────────────


async def test_create_and_get_conversation(env):
    """create persists a conversation; get retrieves it."""
    conv = _conv("conv-1")
    await env.repo.create(conv)
    fetched = await env.repo.get("conv-1")
    assert fetched is not None
    assert fetched.id == "conv-1"
    assert fetched.type == ConversationType.DM


async def test_get_missing_conversation(env):
    """get returns None for an unknown conversation id."""
    assert await env.repo.get("no-such-conv") is None


async def test_list_for_user(env):
    """list_for_user returns conversations the user is an active member of."""
    conv = _conv("conv-lu")
    await env.repo.create(conv)
    await env.repo.add_member(_member("conv-lu", "alice"))
    result = await env.repo.list_for_user("alice")
    assert any(c.id == "conv-lu" for c in result)


async def test_list_for_user_excludes_left(env):
    """list_for_user excludes conversations the user has soft-left."""
    conv = _conv("conv-left")
    await env.repo.create(conv)
    await env.repo.add_member(_member("conv-left", "alice"))
    await env.repo.soft_leave("conv-left", "alice")
    result = await env.repo.list_for_user("alice")
    assert not any(c.id == "conv-left" for c in result)


async def test_touch_last_message(env):
    """touch_last_message updates the last_message_at on the conversation."""
    conv = _conv("conv-touch")
    await env.repo.create(conv)
    ts = "2025-06-01T12:00:00"
    await env.repo.touch_last_message("conv-touch", at=ts)
    fetched = await env.repo.get("conv-touch")
    assert fetched.last_message_at is not None


# ── Members ────────────────────────────────────────────────────────────────


async def test_add_and_list_members(env):
    """add_member adds a member; list_members returns them."""
    conv = _conv("conv-m")
    await env.repo.create(conv)
    await env.repo.add_member(_member("conv-m", "alice"))
    await env.repo.add_member(_member("conv-m", "bob"))
    members = await env.repo.list_members("conv-m")
    usernames = {m.username for m in members}
    assert usernames == {"alice", "bob"}


async def test_add_and_list_remote_members(env):
    """add_remote_member persists; list_remote_members retrieves remote participants."""
    conv = _conv("conv-remote")
    await env.repo.create(conv)
    remote = RemoteConversationMember(
        conversation_id="conv-remote",
        instance_id="inst-far",
        remote_username="carol",
        joined_at=datetime.now(timezone.utc).isoformat(),
    )
    await env.repo.add_remote_member(remote)
    remotes = await env.repo.list_remote_members("conv-remote")
    assert len(remotes) == 1
    assert remotes[0].remote_username == "carol"


async def test_set_last_read(env):
    """set_last_read updates the member's last_read_at."""
    conv = _conv("conv-read")
    await env.repo.create(conv)
    await env.repo.add_member(_member("conv-read", "alice"))
    await env.repo.set_last_read("conv-read", "alice", at="2025-06-01T12:00:00")
    members = await env.repo.list_members("conv-read")
    alice = next(m for m in members if m.username == "alice")
    assert alice.last_read_at is not None


# ── Messages ───────────────────────────────────────────────────────────────


async def test_save_and_get_message(env):
    """save_message persists; get_message retrieves a message."""
    conv = _conv("conv-msg")
    await env.repo.create(conv)
    msg = _message("msg-1", "conv-msg", content="Hello!")
    await env.repo.save_message(msg)
    fetched = await env.repo.get_message("msg-1")
    assert fetched is not None
    assert fetched.content == "Hello!"


async def test_get_missing_message(env):
    """get_message returns None for an unknown message id."""
    assert await env.repo.get_message("nope") is None


async def test_list_messages(env):
    """list_messages returns messages for the conversation in reverse chronological order."""
    conv = _conv("conv-list")
    await env.repo.create(conv)
    for i in range(3):
        await env.repo.save_message(_message(f"lmsg-{i}", "conv-list", content=f"m{i}"))
    msgs = await env.repo.list_messages("conv-list")
    assert len(msgs) == 3


async def test_soft_delete_message(env):
    """soft_delete_message marks a message deleted with empty content."""
    conv = _conv("conv-del")
    await env.repo.create(conv)
    await env.repo.save_message(_message("dmsg-1", "conv-del", content="bye"))
    await env.repo.soft_delete_message("dmsg-1")
    fetched = await env.repo.get_message("dmsg-1")
    assert fetched.deleted is True
    assert fetched.content == ""


async def test_edit_message(env):
    """edit_message updates the message content and sets edited_at."""
    conv = _conv("conv-edit")
    await env.repo.create(conv)
    await env.repo.save_message(_message("emsg-1", "conv-edit", content="old"))
    await env.repo.edit_message("emsg-1", "new content")
    fetched = await env.repo.get_message("emsg-1")
    assert fetched.content == "new content"
    assert fetched.edited_at is not None


async def test_count_unread(env):
    """count_unread returns the number of messages newer than last_read_at."""
    conv = _conv("conv-unread")
    await env.repo.create(conv)
    await env.repo.add_member(_member("conv-unread", "bob"))
    # Alice sends 2 messages — bob hasn't read them
    for i in range(2):
        await env.repo.save_message(
            _message(f"urmsg-{i}", "conv-unread", sender="uid-alice")
        )
    count = await env.repo.count_unread("conv-unread", "bob")
    assert count == 2


# ── Reactions ─────────────────────────────────────────────────────────────


async def test_add_and_list_reactions(env):
    """add_reaction persists; list_reactions retrieves reactions for a message."""
    conv = _conv("conv-react")
    await env.repo.create(conv)
    await env.repo.save_message(_message("rmsg-1", "conv-react"))
    await env.repo.add_reaction("rmsg-1", "uid-alice", "👍")
    await env.repo.add_reaction("rmsg-1", "uid-bob", "👍")
    reactions = await env.repo.list_reactions("rmsg-1")
    assert len(reactions) == 2


async def test_remove_reaction(env):
    """remove_reaction deletes the specified user's reaction."""
    conv = _conv("conv-rm-react")
    await env.repo.create(conv)
    await env.repo.save_message(_message("rmmsg-1", "conv-rm-react"))
    await env.repo.add_reaction("rmmsg-1", "uid-alice", "❤️")
    await env.repo.remove_reaction("rmmsg-1", "uid-alice", "❤️")
    reactions = await env.repo.list_reactions("rmmsg-1")
    assert reactions == []


# ── DM history sync helpers ──────────────────────────────────────────────


async def test_list_messages_since_returns_ascending_after_cursor(env):
    conv = _conv("conv-hist")
    await env.repo.create(conv)
    # Seed three messages with explicit timestamps.
    for i, stamp in enumerate(
        [
            "2026-04-01T00:00:00+00:00",
            "2026-04-01T00:01:00+00:00",
            "2026-04-01T00:02:00+00:00",
        ]
    ):
        msg = ConversationMessage(
            id=f"h-{i}",
            conversation_id="conv-hist",
            sender_user_id="uid-alice",
            content=f"msg {i}",
            created_at=datetime.fromisoformat(stamp),
        )
        await env.repo.save_message(msg)
    result = await env.repo.list_messages_since(
        "conv-hist",
        "2026-04-01T00:00:00+00:00",
    )
    assert [m.id for m in result] == ["h-1", "h-2"]


async def test_list_messages_since_none_returns_everything(env):
    conv = _conv("conv-hist-all")
    await env.repo.create(conv)
    await env.repo.save_message(_message("h-a", "conv-hist-all"))
    await env.repo.save_message(_message("h-b", "conv-hist-all"))
    result = await env.repo.list_messages_since("conv-hist-all", None)
    assert {m.id for m in result} == {"h-a", "h-b"}


async def test_list_conversations_with_remote_member(env):
    conv_a = _conv("conv-a")
    conv_b = _conv("conv-b")
    await env.repo.create(conv_a)
    await env.repo.create(conv_b)
    await env.repo.add_remote_member(
        RemoteConversationMember(
            conversation_id="conv-a",
            instance_id="peer-x",
            remote_username="x1",
            joined_at=datetime.now(timezone.utc).isoformat(),
        )
    )
    await env.repo.add_remote_member(
        RemoteConversationMember(
            conversation_id="conv-a",
            instance_id="peer-x",
            remote_username="x2",
            joined_at=datetime.now(timezone.utc).isoformat(),
        )
    )
    await env.repo.add_remote_member(
        RemoteConversationMember(
            conversation_id="conv-b",
            instance_id="peer-y",
            remote_username="y1",
            joined_at=datetime.now(timezone.utc).isoformat(),
        )
    )
    result = await env.repo.list_conversations_with_remote_member("peer-x")
    assert result == ["conv-a"]
