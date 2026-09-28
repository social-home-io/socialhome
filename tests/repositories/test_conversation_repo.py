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


async def test_list_for_user_drops_dm_with_blocked_peer(env):
    """list_for_user hides DMs whose other peer the viewer has blocked."""
    conv = _conv("conv-dm-blocked")
    await env.repo.create(conv)
    await env.repo.add_member(_member("conv-dm-blocked", "alice"))
    await env.repo.add_member(_member("conv-dm-blocked", "bob"))

    # Without a block alice sees the DM.
    result = await env.repo.list_for_user("alice")
    assert any(c.id == "conv-dm-blocked" for c in result)

    # After alice blocks bob, the DM is hidden from her thread list.
    await env.db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES(?, ?)",
        ("uid-alice", "uid-bob"),
    )
    result = await env.repo.list_for_user("alice")
    assert not any(c.id == "conv-dm-blocked" for c in result)
    # bob still sees the thread — block is asymmetric.
    result = await env.repo.list_for_user("bob")
    assert any(c.id == "conv-dm-blocked" for c in result)


async def test_list_for_user_keeps_groups_with_blocked_member(env):
    """Group DMs stay visible even if one member is blocked — v1 limitation."""
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("carol", "uid-carol", "Carol"),
    )
    conv = _conv("conv-grp", type=ConversationType.GROUP_DM)
    await env.repo.create(conv)
    await env.repo.add_member(_member("conv-grp", "alice"))
    await env.repo.add_member(_member("conv-grp", "bob"))
    await env.repo.add_member(_member("conv-grp", "carol"))
    await env.db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES(?, ?)",
        ("uid-alice", "uid-bob"),
    )
    result = await env.repo.list_for_user("alice")
    assert any(c.id == "conv-grp" for c in result)


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


# ── Delivery state (§12.5) ────────────────────────────────────────────────


async def _seed_conv_with_msg(env, *, conv_id="c1", msg_id="m1"):
    await env.repo.create(_conv(conv_id))
    msg = ConversationMessage(
        id=msg_id,
        conversation_id=conv_id,
        sender_user_id="uid-alice",
        content="hi",
        created_at=datetime.now(timezone.utc),
    )
    await env.repo.save_message(msg)


async def test_upsert_delivery_state_delivered_then_read(env):
    await _seed_conv_with_msg(env)
    await env.repo.upsert_delivery_state(
        conversation_id="c1",
        message_id="m1",
        user_id="uid-bob",
        state="delivered",
    )
    rows = await env.repo.list_delivery_states("c1")
    assert len(rows) == 1
    assert rows[0]["state"] == "delivered"

    await env.repo.upsert_delivery_state(
        conversation_id="c1",
        message_id="m1",
        user_id="uid-bob",
        state="read",
    )
    rows = await env.repo.list_delivery_states("c1")
    assert rows[0]["state"] == "read"


async def test_read_does_not_downgrade_to_delivered(env):
    await _seed_conv_with_msg(env)
    await env.repo.upsert_delivery_state(
        conversation_id="c1",
        message_id="m1",
        user_id="uid-bob",
        state="read",
    )
    # Delayed ``delivered`` ack arriving after the user already opened
    # the conversation must not flip the state back.
    await env.repo.upsert_delivery_state(
        conversation_id="c1",
        message_id="m1",
        user_id="uid-bob",
        state="delivered",
    )
    rows = await env.repo.list_delivery_states("c1")
    assert rows[0]["state"] == "read"


async def test_list_delivery_states_filters_by_message_ids(env):
    await _seed_conv_with_msg(env, msg_id="m1")
    await env.repo.save_message(
        ConversationMessage(
            id="m2",
            conversation_id="c1",
            sender_user_id="uid-alice",
            content="hi2",
            created_at=datetime.now(timezone.utc),
        )
    )
    await env.repo.upsert_delivery_state(
        conversation_id="c1", message_id="m1", user_id="uid-bob", state="read"
    )
    await env.repo.upsert_delivery_state(
        conversation_id="c1", message_id="m2", user_id="uid-bob", state="delivered"
    )
    rows = await env.repo.list_delivery_states("c1", message_ids=["m2"])
    assert len(rows) == 1
    assert rows[0]["message_id"] == "m2"
    # Empty list short-circuits to [].
    assert await env.repo.list_delivery_states("c1", message_ids=[]) == []


async def test_mark_conversation_read_skips_own_messages(env):
    await env.repo.create(_conv("c1"))
    # Alice (self) sends one, bob sends two.
    for msg_id, sender in (
        ("m-own", "uid-alice"),
        ("m-b1", "uid-bob"),
        ("m-b2", "uid-bob"),
    ):
        await env.repo.save_message(
            ConversationMessage(
                id=msg_id,
                conversation_id="c1",
                sender_user_id=sender,
                content="x",
                created_at=datetime.now(timezone.utc),
            )
        )
    up_to = datetime.now(timezone.utc).isoformat()
    marked = await env.repo.mark_conversation_read(
        conversation_id="c1", user_id="uid-alice", up_to_at=up_to
    )
    # Only bob's two messages get flipped — not alice's own.
    assert marked == 2
    rows = await env.repo.list_delivery_states("c1")
    assert all(r["state"] == "read" for r in rows)
    assert {r["message_id"] for r in rows} == {"m-b1", "m-b2"}


async def test_upsert_delivery_state_rejects_invalid_state(env):
    await _seed_conv_with_msg(env)
    with pytest.raises(ValueError):
        await env.repo.upsert_delivery_state(
            conversation_id="c1",
            message_id="m1",
            user_id="uid-bob",
            state="seen",
        )


# ── Race-safe save_message_returning_created ──────────────────────────────


async def test_save_message_returning_created_reports_insert_then_update(env):
    """First call inserts → True; second call to the same id is an
    update → False. Used by the federation inbound DM handler to
    distinguish a brand-new message from a redelivered envelope so
    the notification only fires once."""
    await env.repo.create(_conv("conv-rc"))
    msg = _message("m-rc-1", "conv-rc", content="hi")
    _, created = await env.repo.save_message_returning_created(msg)
    assert created is True

    # Same id, different content (e.g. an edit / voice-note transcript).
    msg2 = _message("m-rc-1", "conv-rc", content="hi (edited)")
    _, created2 = await env.repo.save_message_returning_created(msg2)
    assert created2 is False

    # The UPDATE path applied — content was patched.
    fetched = await env.repo.get_message("m-rc-1")
    assert fetched is not None
    assert fetched.content == "hi (edited)"


async def test_save_message_returning_created_is_race_safe(env):
    """Two concurrent transports (perfect-negotiation WebRTC +
    HTTPS-inbox failover) racing the same DM_MESSAGE envelope: only
    one call returns ``created=True``. Without ``BEGIN IMMEDIATE``
    around INSERT OR IGNORE + UPDATE, both could see "no existing
    row" and both publish DmMessageCreated → user gets two
    notifications for one message."""
    import asyncio

    await env.repo.create(_conv("conv-race"))

    # Schedule N concurrent saves of the same message id.
    results = await asyncio.gather(
        *[
            env.repo.save_message_returning_created(
                _message("m-race-1", "conv-race", content="hello")
            )
            for _ in range(5)
        ]
    )
    created_flags = [created for _, created in results]
    assert sum(created_flags) == 1, (
        f"expected exactly one INSERT, got {sum(created_flags)} "
        f"out of {len(created_flags)} concurrent saves"
    )


# ── Insert-only catch-up write ────────────────────────────────────────────


async def test_insert_message_if_absent_never_rewrites_an_existing_row(env):
    await env.repo.create(_conv("conv-io"))
    first = _message("m-io-1", "conv-io", content="original")
    assert await env.repo.insert_message_if_absent(first) is True
    again = _message("m-io-1", "conv-io", content="rewritten")
    assert await env.repo.insert_message_if_absent(again) is False
    fetched = await env.repo.get_message("m-io-1")
    assert fetched is not None
    assert fetched.content == "original"


# ── Group roster snapshots (v_37) ─────────────────────────────────────────


def _group(version: int, name: str | None = "Crew") -> Conversation:
    return Conversation(
        id="g1",
        type=ConversationType.GROUP_DM,
        name=name,
        created_at=datetime.now(timezone.utc),
        membership_version=version,
    )


def _seat(instance_id: str, username: str, user_id: str) -> RemoteConversationMember:
    return RemoteConversationMember(
        conversation_id="g1",
        instance_id=instance_id,
        remote_username=username,
        joined_at="2026-09-01T00:00:00+00:00",
        user_id=user_id,
        display_name=username.title(),
    )


async def test_apply_group_roster_creates_the_group_with_its_seats(env):
    change = await env.repo.apply_group_roster(
        _group(1),
        local_usernames=["alice"],
        remote_members=[_seat("inst-c", "carol", "u-carol")],
        at="2026-09-01T00:00:00+00:00",
    )
    assert change is not None
    assert change.created is True
    assert change.added_local == ("alice",)
    conv = await env.repo.get("g1")
    assert conv.type is ConversationType.GROUP_DM
    assert conv.membership_version == 1
    assert conv.name == "Crew"
    seats = await env.repo.list_remote_members("g1")
    assert [
        (s.instance_id, s.remote_username, s.user_id, s.display_name) for s in seats
    ] == [("inst-c", "carol", "u-carol", "Carol")]


async def test_apply_group_roster_refuses_a_stale_or_replayed_version(env):
    await env.repo.apply_group_roster(
        _group(3),
        local_usernames=["alice", "bob"],
        remote_members=[],
        at="t",
    )
    for stale in (3, 2):
        assert (
            await env.repo.apply_group_roster(
                _group(stale, name="Old"),
                local_usernames=["alice"],
                remote_members=[_seat("inst-c", "carol", "u-carol")],
                at="t",
            )
            is None
        )
    conv = await env.repo.get("g1")
    assert conv.membership_version == 3 and conv.name == "Crew"
    active = [m.username for m in await env.repo.list_members("g1") if not m.deleted_at]
    assert sorted(active) == ["alice", "bob"]
    assert await env.repo.list_remote_members("g1") == []


async def test_apply_group_roster_removes_and_brings_back_members(env):
    await env.repo.apply_group_roster(
        _group(1),
        local_usernames=["alice", "bob"],
        remote_members=[_seat("inst-c", "carol", "u-carol")],
        at="t1",
    )
    await env.repo.set_last_read("g1", "alice", at="2026-09-02T00:00:00+00:00")
    change = await env.repo.apply_group_roster(
        _group(2, name="Renamed"),
        local_usernames=["alice"],
        remote_members=[],
        at="t2",
    )
    assert change.removed_local == ("bob",)
    assert change.removed_remote == (("inst-c", "carol"),)
    members = {m.username: m for m in await env.repo.list_members("g1")}
    assert members["bob"].deleted_at == "t2"
    # The watermark of a member who stays is untouched.
    assert members["alice"].last_read_at == "2026-09-02T00:00:00+00:00"
    assert await env.repo.list_remote_members("g1") == []
    assert (await env.repo.get("g1")).name == "Renamed"
    back = await env.repo.apply_group_roster(
        _group(3), local_usernames=["alice", "bob"], remote_members=[], at="t3"
    )
    assert back.added_local == ("bob",)
    members = {m.username: m for m in await env.repo.list_members("g1")}
    assert members["bob"].deleted_at is None


async def test_add_remote_member_keeps_roster_identity_on_a_plain_upsert(env):
    await env.repo.create(_conv("c9"))
    await env.repo.add_remote_member(
        RemoteConversationMember(
            conversation_id="c9",
            instance_id="inst-c",
            remote_username="carol",
            joined_at="t",
            user_id="u-carol",
            display_name="Carol",
        )
    )
    await env.repo.add_remote_member(
        RemoteConversationMember(
            conversation_id="c9",
            instance_id="inst-c",
            remote_username="carol",
            joined_at="t",
        )
    )
    (seat,) = await env.repo.list_remote_members("c9")
    assert (seat.user_id, seat.display_name) == ("u-carol", "Carol")


async def test_soft_delete_messages_by_sender_clears_only_theirs(env):
    await env.repo.create(_conv("c5", ConversationType.GROUP_DM))
    await env.repo.create(_conv("c6"))
    for msg in (
        _message("m1", "c5", sender="u-gone"),
        _message("m2", "c5", sender="u-gone"),
        _message("m3", "c5", sender="uid-alice"),
        _message("m4", "c6", sender="u-gone"),
    ):
        await env.repo.save_message(msg)
    assert await env.repo.soft_delete_messages_by_sender("c5", "u-gone") == 2
    assert await env.repo.soft_delete_messages_by_sender("c5", "u-gone") == 0
    by_id = {m.id: m for m in await env.repo.list_messages("c5")}
    assert by_id["m1"].deleted and by_id["m1"].content == ""
    assert not by_id["m3"].deleted
    assert not (await env.repo.get_message("m4")).deleted


async def test_roster_seats_carry_the_version_they_joined_at(env):
    await env.repo.apply_group_roster(
        _group(1),
        local_usernames=["alice"],
        remote_members=[_seat("i", "c", "u-c")],
        at="t",
    )
    await env.repo.soft_leave("g1", "alice", left_version=1)
    (alice,) = await env.repo.list_members("g1")
    assert (alice.joined_version, alice.left_version) == (1, 1)
    await env.repo.apply_group_roster(
        _group(4),
        local_usernames=["alice", "bob"],
        remote_members=[_seat("i", "c", "u-c")],
        at="t",
    )
    members = {m.username: m for m in await env.repo.list_members("g1")}
    assert (members["alice"].joined_version, members["alice"].left_version) == (4, None)
    assert members["bob"].joined_version == 4
    (seat,) = await env.repo.list_remote_members("g1")
    assert seat.joined_version == 1  # still the same seat since v1
