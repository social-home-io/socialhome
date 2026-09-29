"""Tests for socialhome.domain.conversation — Conversation, ConversationMessage, etc."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.conversation import (
    MUTE_DURATIONS,
    MUTED_FOREVER,
    Conversation,
    ConversationMember,
    ConversationMessage,
    ConversationType,
    MESSAGE_TYPES,
    RemoteConversationMember,
    mute_active,
    mute_until_for,
)


# ── Mute ─────────────────────────────────────────────────────────────────────


def test_mute_until_for_presets():
    now = datetime(2026, 9, 29, 10, 0, 0, 123456, tzinfo=timezone.utc)
    assert mute_until_for("1h", now=now) == "2026-09-29T11:00:00+00:00"
    assert mute_until_for("8h", now=now) == "2026-09-29T18:00:00+00:00"
    assert mute_until_for("1w", now=now) == "2026-10-06T10:00:00+00:00"
    assert mute_until_for("forever", now=now) == MUTED_FOREVER
    assert set(MUTE_DURATIONS) == {"1h", "8h", "1w", "forever"}


def test_mute_until_for_normalises_to_utc():
    cest = timezone(timedelta(hours=2))
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=cest)
    assert mute_until_for("1h", now=now) == "2026-09-29T11:00:00+00:00"


def test_mute_until_for_rejects_unknown_length():
    with pytest.raises(ValueError):
        mute_until_for("2d", now=datetime.now(timezone.utc))


def test_mute_active_reads_past_as_unmuted():
    now = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
    assert mute_active("2026-09-29T11:00:00+00:00", now=now) is True
    assert mute_active("2026-09-29T09:59:59+00:00", now=now) is False
    assert mute_active("2026-09-29T10:00:00+00:00", now=now) is False
    assert mute_active(MUTED_FOREVER, now=now) is True
    assert mute_active(None, now=now) is False
    assert mute_active("", now=now) is False
    assert mute_active("not a time", now=now) is False
    # A naive stamp is UTC.
    assert mute_active("2026-09-29 11:00:00", now=now) is True


def test_conversation_member_defaults_to_unmuted():
    m = ConversationMember(conversation_id="c", username="u", joined_at="x")
    assert m.muted_until is None


def test_message_types_is_frozenset():
    """MESSAGE_TYPES is a frozenset of expected strings."""
    assert isinstance(MESSAGE_TYPES, frozenset)
    assert "text" in MESSAGE_TYPES
    assert "image" in MESSAGE_TYPES
    assert "video" in MESSAGE_TYPES
    assert "transcript" in MESSAGE_TYPES
    assert "location" in MESSAGE_TYPES


def test_conversation_dm():
    """Conversation can be constructed as a DM."""
    now = datetime.now(timezone.utc)
    conv = Conversation(id="conv-1", type=ConversationType.DM, created_at=now)
    assert conv.id == "conv-1"
    assert conv.type == ConversationType.DM
    assert conv.name is None
    assert conv.last_message_at is None
    assert conv.bot_enabled is False


def test_conversation_group_dm():
    """Conversation with GROUP_DM type can carry a name."""
    now = datetime.now(timezone.utc)
    conv = Conversation(
        id="conv-2",
        type=ConversationType.GROUP_DM,
        created_at=now,
        name="Family Chat",
    )
    assert conv.type == ConversationType.GROUP_DM
    assert conv.name == "Family Chat"


def test_conversation_type_string_values():
    """ConversationType string values match the spec."""
    assert ConversationType.DM == "dm"
    assert ConversationType.GROUP_DM == "group_dm"


def test_conversation_message_construction():
    """ConversationMessage can be constructed with required fields and defaults."""
    now = datetime.now(timezone.utc)
    msg = ConversationMessage(
        id="msg-1",
        conversation_id="conv-1",
        sender_user_id="uid-alice",
        content="Hello!",
        created_at=now,
    )
    assert msg.type == "text"
    assert msg.media_url is None
    assert msg.reply_to_id is None
    assert msg.deleted is False
    assert msg.edited_at is None


def test_conversation_message_soft_delete():
    """soft_delete returns a new message with content cleared and deleted=True."""
    now = datetime.now(timezone.utc)
    msg = ConversationMessage(
        id="msg-1",
        conversation_id="conv-1",
        sender_user_id="uid-alice",
        content="Hello!",
        created_at=now,
        media_url="http://example.com/img.png",
    )
    deleted = msg.soft_delete()
    assert deleted.deleted is True
    assert deleted.content == ""
    assert deleted.media_url is None
    # Original unchanged
    assert msg.deleted is False
    assert msg.content == "Hello!"


def test_conversation_message_edit():
    """edit returns a new message with updated content and edited_at set."""
    now = datetime.now(timezone.utc)
    msg = ConversationMessage(
        id="msg-1",
        conversation_id="conv-1",
        sender_user_id="uid-alice",
        content="Hello!",
        created_at=now,
    )
    edited = msg.edit("Updated content")
    assert edited.content == "Updated content"
    assert edited.edited_at is not None
    # Original unchanged
    assert msg.content == "Hello!"
    assert msg.edited_at is None


def test_conversation_message_edit_with_explicit_now():
    """edit uses the provided 'now' datetime for edited_at."""
    now = datetime.now(timezone.utc)
    edit_time = datetime(2025, 6, 1, tzinfo=timezone.utc)
    msg = ConversationMessage(
        id="msg-1",
        conversation_id="conv-1",
        sender_user_id="uid-bob",
        content="old",
        created_at=now,
    )
    edited = msg.edit("new", now=edit_time)
    assert edited.edited_at == edit_time


def test_conversation_member_construction():
    """ConversationMember can be constructed with required fields."""
    member = ConversationMember(
        conversation_id="conv-1",
        username="alice",
        joined_at="2025-01-01T00:00:00",
    )
    assert member.username == "alice"
    assert member.last_read_at is None
    assert member.deleted_at is None


def test_remote_conversation_member_construction():
    """RemoteConversationMember can be constructed with required fields."""
    remote = RemoteConversationMember(
        conversation_id="conv-1",
        instance_id="inst-abc",
        remote_username="carol",
        joined_at="2025-01-01T00:00:00",
    )
    assert remote.instance_id == "inst-abc"
    assert remote.history_visible_from is None
