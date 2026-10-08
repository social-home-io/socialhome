"""Tests for socialhome.services.space_chat_service — a space's chat, and the
space chat end to end through DmService (policy-gated group-DM storage)."""

from __future__ import annotations

import pytest

from socialhome.domain.conversation import MUTED_FOREVER, SystemChatScope
from socialhome.domain.events import (
    DmMessageCreated,
    DmMessageDeleted,
    SpaceConfigChanged,
    SpaceMemberJoined,
    SpaceMemberLeft,
)
from socialhome.domain.preferences import FeatureDisabledError
from socialhome.domain.space import SpaceArchivedError
from socialhome.federation.owner_bound_id import (
    SPACE_CHAT_MESSAGE_KIND,
    OwnerBinding,
    check_owner_bound_id,
)

from .space_chat_stack import SP, build_stack


@pytest.fixture
async def stack(tmp_dir):
    s = await build_stack(tmp_dir)
    yield s
    await s.db.shutdown()


async def _seated(s, chat_id: str) -> set[str]:
    return {
        m.username for m in await s.convos.list_members(chat_id) if not m.deleted_at
    }


# ── Lifecycle + reconciler ────────────────────────────────────────────────


async def test_ensure_chat_is_idempotent_and_refuses_unknown_spaces(stack):
    assert await stack.chat.get_chat(SP) is None
    first = await stack.chat.ensure_chat(SP)
    again = await stack.chat.ensure_chat(SP)
    assert first.id == again.id
    assert first.system_scope is SystemChatScope.SPACE and first.space_id == SP
    with pytest.raises(KeyError):
        await stack.chat.ensure_chat("sp-nowhere")


async def test_reconcile_seats_writers_only(stack):
    chat = await stack.chat.reconcile(SP)
    # Owner, member, moderator — never the follower nor a non-member.
    assert await _seated(stack, chat.id) == {"anna", "bob", "mod"}
    (seat,) = [
        m for m in await stack.convos.list_members(chat.id) if m.username == "bob"
    ]
    assert seat.notif_level == "mentions"


async def test_reconciler_follows_joins_leaves_and_role_changes(stack):
    chat = await stack.chat.reconcile(SP)
    await stack.db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?,?,'member')",
        (SP, "u-zoe"),
    )
    await stack.bus.publish(SpaceMemberJoined(space_id=SP, user_id="u-zoe"))
    assert "zoe" in await _seated(stack, chat.id)
    # Demoted to follower: out, on the role change.
    await stack.db.enqueue(
        "UPDATE space_members SET role='subscriber' WHERE user_id='u-bob'"
    )
    await stack.bus.publish(
        SpaceConfigChanged(
            space_id=SP, event_type="role_changed", payload={}, sequence=0
        )
    )
    assert "bob" not in await _seated(stack, chat.id)
    await stack.spaces.delete_member(SP, "u-zoe")
    await stack.bus.publish(SpaceMemberLeft(space_id=SP, user_id="u-zoe"))
    assert await _seated(stack, chat.id) == {"anna", "mod"}


async def test_space_events_before_the_chat_exists_create_nothing(stack):
    await stack.bus.publish(SpaceMemberJoined(space_id=SP, user_id="u-bob"))
    assert await stack.chat.get_chat(SP) is None


async def test_banned_member_loses_the_seat(stack):
    chat = await stack.chat.reconcile(SP)
    await stack.spaces.ban_member(SP, "u-bob", banned_by="u-anna")
    await stack.bus.publish(
        SpaceConfigChanged(
            space_id=SP, event_type="member_banned", payload={}, sequence=0
        )
    )
    assert "bob" not in await _seated(stack, chat.id)


# ── Summary ───────────────────────────────────────────────────────────────


async def test_summary_for_a_member(stack):
    summary = await stack.chat.summary(SP, "bob")
    assert summary.enabled and summary.conversation_id
    assert summary.unread == 0 and summary.notif_level == "mentions"
    assert summary.muted_until is None and summary.last_read_at is not None


async def test_summary_reports_unread_and_mute(stack):
    chat_id = (await stack.chat.summary(SP, "bob")).conversation_id
    await stack.dm.send_message(chat_id, sender_username="anna", content="hi all")
    await stack.dm.mute(chat_id, username="bob", duration="forever")
    summary = await stack.chat.summary(SP, "bob")
    assert summary.unread == 1 and summary.muted_until == MUTED_FOREVER


async def test_summary_follower_and_chat_off_are_disabled(stack):
    follower = await stack.chat.summary(SP, "finn")
    assert not follower.enabled and follower.conversation_id is None
    await stack.db.enqueue("UPDATE spaces SET feature_chat=0 WHERE id=?", (SP,))
    off = await stack.chat.summary(SP, "bob")
    assert not off.enabled and off.conversation_id is None
    assert await stack.chat.get_chat(SP) is None


@pytest.mark.parametrize("who", ["zoe", "nobody"])
async def test_summary_404s_for_non_members(stack, who):
    with pytest.raises(KeyError):
        await stack.chat.summary(SP, who)


async def test_summary_404s_for_banned_dissolved_and_unknown(stack):
    await stack.spaces.ban_member(SP, "u-mod", banned_by="u-anna")
    with pytest.raises(KeyError):
        await stack.chat.summary(SP, "mod")
    with pytest.raises(KeyError):
        await stack.chat.summary("sp-nowhere", "bob")
    await stack.db.enqueue("UPDATE spaces SET dissolved=1 WHERE id=?", (SP,))
    with pytest.raises(KeyError):
        await stack.chat.summary(SP, "bob")


async def test_summary_of_an_inactive_member_is_disabled(stack):
    await stack.db.enqueue("UPDATE users SET state='inactive' WHERE username='bob'")
    assert not (await stack.chat.summary(SP, "bob")).enabled


# ── End to end through DmService ──────────────────────────────────────────


async def test_send_mints_an_owner_bound_id_and_reaches_the_writers(stack):
    chat_id = (await stack.chat.summary(SP, "anna")).conversation_id
    msg = await stack.dm.send_message(chat_id, sender_username="anna", content="hi")
    assert (
        check_owner_bound_id(
            SPACE_CHAT_MESSAGE_KIND, msg.id, space_id=SP, owner_user_id="u-anna"
        )
        is OwnerBinding.VALID
    )
    (created,) = stack.events(DmMessageCreated)
    assert created.system_scope == "space" and created.origin_instance_id is None
    assert set(created.recipient_user_ids) == {"u-bob", "u-mod"}


async def test_a_demoted_seat_is_not_in_the_audience_before_reconciling(stack):
    chat_id = (await stack.chat.summary(SP, "anna")).conversation_id
    # The seat is still there; the live policy keeps bob out anyway.
    await stack.db.enqueue(
        "UPDATE space_members SET role='subscriber' WHERE user_id='u-bob'"
    )
    await stack.dm.send_message(chat_id, sender_username="anna", content="hi")
    (created,) = stack.events(DmMessageCreated)
    assert created.recipient_user_ids == ("u-mod",)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"type": "location", "content": '{"lat": 1, "lon": 2}'},
        {"type": "image", "media_url": "api/media/a.webp"},
        {"reply_to_highlight_frame_id": "frame-1"},
    ],
)
async def test_space_chat_is_text_only(stack, kwargs):
    chat_id = (await stack.chat.summary(SP, "anna")).conversation_id
    with pytest.raises(ValueError):
        await stack.dm.send_message(
            chat_id, sender_username="anna", **{"content": "x", **kwargs}
        )


async def test_a_reply_must_name_a_message_of_the_same_chat(stack):
    chat_id = (await stack.chat.summary(SP, "anna")).conversation_id
    first = await stack.dm.send_message(chat_id, sender_username="anna", content="q")
    reply = await stack.dm.send_message(
        chat_id, sender_username="bob", content="a", reply_to_id=first.id
    )
    assert reply.reply_to_id == first.id
    with pytest.raises(ValueError):
        await stack.dm.send_message(
            chat_id, sender_username="bob", content="a", reply_to_id="nope"
        )


async def test_followers_and_strangers_cannot_post_or_read(stack):
    chat_id = (await stack.chat.summary(SP, "anna")).conversation_id
    for who in ("finn", "zoe"):
        with pytest.raises(PermissionError):
            await stack.dm.send_message(chat_id, sender_username=who, content="x")
        with pytest.raises(PermissionError):
            await stack.dm.list_messages(chat_id, reader_username=who)


async def test_chat_off_refuses_and_archived_refuses_writes_only(stack):
    chat_id = (await stack.chat.summary(SP, "anna")).conversation_id
    msg = await stack.dm.send_message(chat_id, sender_username="bob", content="x")
    await stack.db.enqueue("UPDATE spaces SET archived=1 WHERE id=?", (SP,))
    assert await stack.dm.list_messages(chat_id, reader_username="bob")
    with pytest.raises(SpaceArchivedError):
        await stack.dm.send_message(chat_id, sender_username="bob", content="y")
    # The author may still delete their own message in an archived space.
    await stack.dm.delete_message(msg.id, actor_username="bob")
    await stack.db.enqueue("UPDATE spaces SET feature_chat=0 WHERE id=?", (SP,))
    with pytest.raises(FeatureDisabledError):
        await stack.dm.list_messages(chat_id, reader_username="bob")


async def test_moderators_delete_anyones_message_members_do_not(stack):
    chat_id = (await stack.chat.summary(SP, "anna")).conversation_id
    by_bob = await stack.dm.send_message(chat_id, sender_username="bob", content="x")
    by_anna = await stack.dm.send_message(chat_id, sender_username="anna", content="y")
    with pytest.raises(PermissionError):
        await stack.dm.delete_message(by_anna.id, actor_username="bob")
    await stack.dm.delete_message(by_bob.id, actor_username="mod")
    deleted = stack.events(DmMessageDeleted)
    assert [(d.message_id, d.actor_user_id, d.sender_user_id) for d in deleted] == [
        (by_bob.id, "u-mod", "u-bob")
    ]
    assert deleted[0].system_scope == "space"
    # Every local reader's open thread — the moderator's other tabs too.
    assert set(deleted[0].recipient_user_ids) == {"u-anna", "u-bob", "u-mod"}
    stored = await stack.convos.get_message(by_bob.id)
    assert stored is not None and stored.deleted
