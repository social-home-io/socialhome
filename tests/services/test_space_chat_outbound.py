"""Tests for socialhome.services.space_chat_outbound — a space chat's
messages go to its writer households only, version-gated, never echoed."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.domain.events import (
    DmMessageCreated,
    DmMessageDeleted,
    DmMessageReactionChanged,
    DmMessageUpdated,
)
from socialhome.domain.federation import FederationEventType
from socialhome.domain.federation_capabilities import FederationCapability

from .space_chat_stack import HOUSE_B, SP, build_stack

FET = FederationEventType


@pytest.fixture
async def stack(tmp_dir):
    s = await build_stack(tmp_dir)
    yield s
    await s.db.shutdown()


async def _chat(stack) -> str:
    summary = await stack.chat.summary(SP, "anna")
    assert summary.conversation_id
    return summary.conversation_id


async def test_writer_households_skip_followers_tombstones_and_ourselves(stack):
    assert await stack.audience.writer_households(SP) == {HOUSE_B}
    assert await stack.audience.may_receive(SP, HOUSE_B)
    assert not await stack.audience.may_receive(SP, "house-f")
    await stack.db.enqueue(
        "UPDATE space_remote_members SET tombstoned=1 WHERE instance_id=?", (HOUSE_B,)
    )
    assert await stack.audience.writer_households(SP) == frozenset()
    # Our own household is never a target, even when mirrored.
    await stack.db.enqueue(
        "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
        " VALUES(?,?,'u-self','member')",
        (SP, stack.iid),
    )
    assert await stack.audience.writer_households(SP) == frozenset()


async def test_create_edit_react_delete_are_broadcast_to_writers(stack):
    chat_id = await _chat(stack)
    msg = await stack.dm.send_message(chat_id, sender_username="anna", content="hi")
    reply = await stack.dm.send_message(
        chat_id, sender_username="bob", content="hey", reply_to_id=msg.id
    )
    await stack.dm.edit_message(msg.id, editor_username="anna", new_content="hi!")
    await stack.dm.add_reaction(msg.id, username="bob", emoji="👍")
    await stack.dm.remove_reaction(msg.id, username="bob", emoji="👍")
    await stack.dm.delete_message(reply.id, actor_username="mod")
    sent = stack.federation.sent
    assert [b.event_type for b in sent] == [
        FET.SPACE_CHAT_MESSAGE_CREATED,
        FET.SPACE_CHAT_MESSAGE_CREATED,
        FET.SPACE_CHAT_MESSAGE_UPDATED,
        FET.SPACE_CHAT_REACTION,
        FET.SPACE_CHAT_REACTION,
        FET.SPACE_CHAT_MESSAGE_DELETED,
    ]
    for b in sent:
        assert b.space_id == SP
        assert b.only_instances == {HOUSE_B}
        # The version gate ran per household above (space_member_supports).
        assert b.min_proto_version is None
        # Each household keeps its own chat: no conversation id travels.
        assert "conversation_id" not in b.payload
        assert chat_id not in str(b.payload)
    create, reply_create, edit, add, remove, delete = (b.payload for b in sent)
    assert create == {
        "space_id": SP,
        "message_id": msg.id,
        "author_user_id": "u-anna",
        "content": "hi",
        "reply_to_id": None,
        "created_at": msg.created_at.isoformat(),
    }
    assert reply_create["reply_to_id"] == msg.id
    assert edit["content"] == "hi!" and edit["author_user_id"] == "u-anna"
    assert (add["action"], remove["action"]) == ("add", "remove")
    assert add["user_id"] == "u-bob" and add["emoji"] == "👍"
    assert delete == {
        "space_id": SP,
        "message_id": reply.id,
        "author_user_id": "u-bob",
        "actor_user_id": "u-mod",
    }


async def test_a_household_below_v55_is_left_out(stack):
    stack.federation.older.add(HOUSE_B)
    chat_id = await _chat(stack)
    await stack.dm.send_message(chat_id, sender_username="anna", content="hi")
    assert stack.federation.sent == []
    assert FederationCapability.MIN_FOR_SPACE_CHAT == 55


async def test_nothing_is_sent_without_a_writer_household(stack):
    await stack.db.enqueue(
        "DELETE FROM space_remote_members WHERE role != 'subscriber'"
    )
    chat_id = await _chat(stack)
    await stack.dm.send_message(chat_id, sender_username="anna", content="hi")
    assert stack.federation.sent == []


async def test_inbound_events_are_never_echoed(stack):
    chat_id = await _chat(stack)
    now = datetime.now(timezone.utc)
    for event in (
        DmMessageCreated(
            conversation_id=chat_id,
            message_id="m",
            sender_user_id="u-rb",
            sender_display_name="RB",
            recipient_user_ids=(),
            system_scope="space",
            origin_instance_id=HOUSE_B,
        ),
        DmMessageUpdated(
            conversation_id=chat_id,
            message_id="m",
            sender_user_id="u-rb",
            recipient_user_ids=(),
            content="x",
            edited_at=now,
            is_edit=True,
            origin_instance_id=HOUSE_B,
        ),
        DmMessageDeleted(
            conversation_id=chat_id,
            message_id="m",
            sender_user_id="u-rb",
            actor_user_id="u-rb",
            system_scope="space",
            origin_instance_id=HOUSE_B,
        ),
        DmMessageReactionChanged(
            conversation_id=chat_id,
            message_id="m",
            user_id="u-rb",
            emoji="👍",
            action="add",
            recipient_user_ids=(),
            origin_instance_id=HOUSE_B,
        ),
    ):
        await stack.bus.publish(event)
    assert stack.federation.sent == []


async def test_dms_household_chat_and_transcripts_are_not_space_chat(stack):
    await stack.db.enqueue("INSERT INTO conversations(id, type) VALUES('dm-1', 'dm')")
    now = datetime.now(timezone.utc)
    for event in (
        # A DM or the household chat: not a space.
        DmMessageCreated(
            conversation_id="dm-1",
            message_id="m",
            sender_user_id="u-anna",
            sender_display_name="A",
            recipient_user_ids=(),
        ),
        DmMessageCreated(
            conversation_id="dm-1",
            message_id="m",
            sender_user_id="u-anna",
            sender_display_name="A",
            recipient_user_ids=(),
            system_scope="space",  # a mislabelled event is checked on the row
        ),
        DmMessageDeleted(
            conversation_id="dm-1",
            message_id="m",
            sender_user_id="u-anna",
            actor_user_id="u-anna",
        ),
        DmMessageDeleted(
            conversation_id="dm-1",
            message_id="m",
            sender_user_id="u-anna",
            actor_user_id="u-anna",
            system_scope="space",
        ),
        DmMessageReactionChanged(
            conversation_id="dm-1",
            message_id="m",
            user_id="u-anna",
            emoji="👍",
            action="add",
            recipient_user_ids=(),
        ),
        DmMessageUpdated(
            conversation_id="dm-1",
            message_id="m",
            sender_user_id="u-anna",
            recipient_user_ids=(),
            content="x",
            edited_at=now,
            is_edit=True,
        ),
        # A transcript patch is not an edit.
        DmMessageUpdated(
            conversation_id=await _chat(stack),
            message_id="m",
            sender_user_id="u-anna",
            recipient_user_ids=(),
            content="x",
            edited_at=now,
        ),
    ):
        await stack.bus.publish(event)
    assert stack.federation.sent == []


async def test_a_failed_broadcast_is_logged_not_raised(stack, caplog):
    async def _boom(*_a, **_kw):
        raise RuntimeError("down")

    stack.federation.broadcast_to_space_members = _boom  # type: ignore[method-assign]
    chat_id = await _chat(stack)
    await stack.dm.send_message(chat_id, sender_username="anna", content="hi")
    assert "space_chat_message_created broadcast failed" in caplog.text
