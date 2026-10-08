"""Tests for socialhome.services.federation_inbound.space_chat — the inbound
``SPACE_CHAT_*`` handlers (the space-chat half of §24.11 authorship)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.events import (
    DmMessageCreated,
    DmMessageDeleted,
    DmMessageReactionChanged,
    DmMessageUpdated,
)
from socialhome.domain.federation import FederationEventType

from ..space_chat_stack import HOUSE_B, HOUSE_F, SP, build_stack, chat_id

FET = FederationEventType


@pytest.fixture
async def stack(tmp_dir):
    s = await build_stack(tmp_dir)
    yield s
    await s.db.shutdown()


async def _rows(stack) -> list[tuple]:
    return [
        (r["id"], r["sender_user_id"], r["content"], r["deleted"])
        for r in await stack.db.fetchall(
            "SELECT id, sender_user_id, content, deleted FROM conversation_messages"
            " ORDER BY rowid"
        )
    ]


async def _create(stack, msg_id, *, author="u-rb", sender=HOUSE_B, **extra):
    await stack.federation.deliver(
        FET.SPACE_CHAT_MESSAGE_CREATED,
        {"message_id": msg_id, "author_user_id": author, "content": "hi", **extra},
        sender=sender,
    )


# ── Create ────────────────────────────────────────────────────────────────


async def test_a_writers_message_lands_and_notifies_local_writers(stack):
    mid = chat_id("u-rb")
    await _create(stack, mid)
    chat = await stack.chat.get_chat(SP)
    assert chat is not None
    stored = await stack.convos.get_message(mid)
    assert stored is not None and stored.conversation_id == chat.id
    assert stored.sender_user_id == "u-rb" and stored.content == "hi"
    (event,) = stack.events(DmMessageCreated)
    assert event.origin_instance_id == HOUSE_B and event.system_scope == "space"
    assert set(event.recipient_user_ids) == {"u-anna", "u-bob", "u-mod"}
    # The seat's display name stands in for a household we never paired with.
    assert event.sender_display_name == "U-RB"


async def test_a_redelivered_message_is_a_no_op(stack):
    mid = chat_id("u-rb")
    await _create(stack, mid)
    await _create(stack, mid)
    assert len(await _rows(stack)) == 1
    assert len(stack.events(DmMessageCreated)) == 1


_REFUSED_CREATES = [
    ("legacy id", "ab" * 16, "u-rb", HOUSE_B),
    ("id bound to another author", chat_id("u-radm"), "u-rb", HOUSE_B),
    ("id bound to another space", chat_id("u-rb", "sp-x"), "u-rb", HOUSE_B),
    ("author not seated on the sender", chat_id("u-rb"), "u-rb", HOUSE_F),
    ("a follower author", chat_id("u-rf"), "u-rf", HOUSE_F),
    ("our own local user", chat_id("u-bob"), "u-bob", HOUSE_B),
    ("the bot identity", chat_id("system-integration"), "system-integration", HOUSE_B),
    ("a stranger household", chat_id("u-rb"), "u-rb", "house-x"),
]


@pytest.mark.parametrize(
    ("label", "payload_id", "author", "sender"),
    _REFUSED_CREATES,
    ids=[c[0] for c in _REFUSED_CREATES],
)
async def test_creates_that_are_refused(stack, label, payload_id, author, sender):
    await _create(stack, payload_id, author=author, sender=sender)
    assert await _rows(stack) == [], label
    assert stack.events(DmMessageCreated) == []


async def test_a_banned_author_is_refused(stack):
    await stack.spaces.ban_member(SP, "u-rb", banned_by="u-anna")
    await _create(stack, chat_id("u-rb"))
    assert await _rows(stack) == []


@pytest.mark.parametrize(
    "setup",
    [
        "UPDATE spaces SET feature_chat=0",
        "UPDATE spaces SET dissolved=1",
        # No local writer: a follower-only household stores no chat.
        "UPDATE space_members SET role='subscriber'",
    ],
)
async def test_nothing_lands_where_the_chat_is_not_ours_to_hold(stack, setup):
    await stack.db.enqueue(setup)
    await _create(stack, chat_id("u-rb"))
    assert await _rows(stack) == []
    assert await stack.chat.get_chat(SP) is None


async def test_a_payload_naming_another_space_is_dropped(stack):
    await stack.federation.deliver(
        FET.SPACE_CHAT_MESSAGE_CREATED,
        {
            "message_id": chat_id("u-rb"),
            "author_user_id": "u-rb",
            "content": "x",
            "space_id": "sp-other",
        },
        sender=HOUSE_B,
    )
    assert await _rows(stack) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"author_user_id": "u-rb", "content": "x"},
        {"message_id": "m", "content": "x"},
        {"message_id": "m", "author_user_id": "u-rb"},
        {"message_id": "m", "author_user_id": "u-rb", "content": 5},
        {"message_id": "m", "author_user_id": "u-rb", "content": "x" * 1001},
    ],
)
async def test_malformed_creates_are_dropped(stack, payload):
    await stack.federation.deliver(
        FET.SPACE_CHAT_MESSAGE_CREATED, payload, sender=HOUSE_B
    )
    assert await _rows(stack) == []


async def test_reply_kept_only_inside_the_chat_and_future_clamped(stack):
    first = chat_id("u-rb")
    await _create(stack, first)
    reply = chat_id("u-rb")
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    await _create(stack, reply, reply_to_id=first, created_at=future)
    stray = chat_id("u-rb")
    await _create(stack, stray, reply_to_id="not-a-message")
    got = await stack.convos.get_message(reply)
    assert got is not None and got.reply_to_id == first
    assert got.created_at <= datetime.now(timezone.utc)
    stray_msg = await stack.convos.get_message(stray)
    assert stray_msg is not None and stray_msg.reply_to_id is None


async def test_the_host_relays_a_remote_writers_message_by_sync(stack):
    """A member household (we are not the host here) takes the host's
    catch-up of another household's writer."""
    await stack.db.enqueue("UPDATE spaces SET owner_instance_id='house-host'")
    await stack.db.enqueue(
        "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
        " VALUES(?, 'house-host', 'u-h', 'member')",
        (SP,),
    )
    mid = chat_id("u-rb")
    await stack.inbound.apply_sync_records(
        SP,
        [{"id": mid, "author_user_id": "u-rb", "content": "old", "created_at": "x"}],
        provider="house-host",
    )
    assert [r[0] for r in await _rows(stack)] == [mid]
    # …but never a follower's.
    await stack.inbound.apply_sync_records(
        SP,
        [{"message_id": chat_id("u-rf"), "author_user_id": "u-rf", "content": "x"}],
        provider="house-host",
    )
    assert len(await _rows(stack)) == 1


# ── Update / delete / reaction ────────────────────────────────────────────


async def test_only_the_authors_household_edits(stack):
    mid = chat_id("u-rb")
    await _create(stack, mid)
    for sender, author in ((HOUSE_F, "u-rb"), (HOUSE_B, "u-radm")):
        await stack.federation.deliver(
            FET.SPACE_CHAT_MESSAGE_UPDATED,
            {"message_id": mid, "author_user_id": author, "content": "hijack"},
            sender=sender,
        )
    assert (await _rows(stack))[0][2] == "hi"
    await stack.federation.deliver(
        FET.SPACE_CHAT_MESSAGE_UPDATED,
        {"message_id": mid, "author_user_id": "u-rb", "content": "hi @bob"},
        sender=HOUSE_B,
    )
    assert (await _rows(stack))[0][2] == "hi @bob"
    (edit,) = stack.events(DmMessageUpdated)
    assert edit.is_edit and edit.origin_instance_id == HOUSE_B
    assert [m.user_id for m in edit.new_mentions] == ["u-bob"]


async def test_an_edit_of_an_unknown_or_foreign_message_changes_nothing(stack):
    await stack.db.enqueue("INSERT INTO conversations(id, type) VALUES('dm', 'dm')")
    await stack.db.enqueue(
        "INSERT INTO conversation_messages(id, conversation_id, sender_user_id,"
        " content) VALUES('dm-m', 'dm', 'u-rb', 'secret')"
    )
    await stack.chat.reconcile(SP)
    for mid in ("dm-m", "nope"):
        await stack.federation.deliver(
            FET.SPACE_CHAT_MESSAGE_UPDATED,
            {"message_id": mid, "author_user_id": "u-rb", "content": "x"},
            sender=HOUSE_B,
        )
    assert await _rows(stack) == [("dm-m", "u-rb", "secret", 0)]
    # Before the chat exists here, an edit is simply unknown.


@pytest.mark.parametrize(
    ("actor", "sender", "lands"),
    [
        ("u-rb", HOUSE_B, True),  # the author
        ("u-radm", HOUSE_B, True),  # a remote admin (content authority)
        ("u-rf", HOUSE_F, False),  # a follower
        ("u-rb", HOUSE_F, False),  # the author named by another household
        ("", HOUSE_B, False),
    ],
)
async def test_delete_by_author_or_content_authority(stack, actor, sender, lands):
    mid = chat_id("u-rb")
    await _create(stack, mid)
    await stack.federation.deliver(
        FET.SPACE_CHAT_MESSAGE_DELETED,
        {"message_id": mid, "actor_user_id": actor},
        sender=sender,
    )
    assert bool((await _rows(stack))[0][3]) is lands
    assert bool(stack.events(DmMessageDeleted)) is lands


async def test_delete_of_a_deleted_message_is_quiet(stack):
    mid = chat_id("u-rb")
    await _create(stack, mid)
    for _ in range(2):
        await stack.federation.deliver(
            FET.SPACE_CHAT_MESSAGE_DELETED,
            {"message_id": mid, "actor_user_id": "u-rb"},
            sender=HOUSE_B,
        )
    assert len(stack.events(DmMessageDeleted)) == 1


async def test_reactions_are_the_reactors_own(stack):
    mid = chat_id("u-rb")
    await _create(stack, mid)
    await stack.federation.deliver(
        FET.SPACE_CHAT_REACTION,
        {"message_id": mid, "user_id": "u-rf", "emoji": "👍", "action": "add"},
        sender=HOUSE_F,
    )
    assert await stack.convos.list_reactions(mid) == []
    await stack.federation.deliver(
        FET.SPACE_CHAT_REACTION,
        {"message_id": mid, "user_id": "u-radm", "emoji": "👍", "action": "add"},
        sender=HOUSE_B,
    )
    assert [r.user_id for r in await stack.convos.list_reactions(mid)] == ["u-radm"]
    await stack.federation.deliver(
        FET.SPACE_CHAT_REACTION,
        {"message_id": mid, "user_id": "u-radm", "emoji": "👍", "action": "remove"},
        sender=HOUSE_B,
    )
    assert await stack.convos.list_reactions(mid) == []
    changes = stack.events(DmMessageReactionChanged)
    assert [c.action for c in changes] == ["add", "remove"]
    assert all(c.origin_instance_id == HOUSE_B for c in changes)


@pytest.mark.parametrize(
    "payload",
    [
        {"user_id": "u-rb", "emoji": "👍", "action": "flip"},
        {"user_id": "u-rb", "emoji": "", "action": "add"},
        {"user_id": "u-rb", "emoji": "x" * 33, "action": "add"},
        {"emoji": "👍", "action": "add"},
    ],
)
async def test_malformed_reactions_are_dropped(stack, payload):
    mid = chat_id("u-rb")
    await _create(stack, mid)
    await stack.federation.deliver(
        FET.SPACE_CHAT_REACTION, {"message_id": mid, **payload}, sender=HOUSE_B
    )
    assert await stack.convos.list_reactions(mid) == []


async def test_without_a_roster_mirror_everything_is_refused(stack):
    stack.inbound._authorship = None
    await _create(stack, chat_id("u-rb"))
    assert await _rows(stack) == []


async def test_the_message_that_creates_the_chat_here_reads_as_unread(stack):
    """Regression: the first inbound message creates the chat and seats the
    local writers JUST BEFORE it — seated at "now", the sender's earlier
    timestamp read as already seen and the bell's unread pill stayed at 0."""
    await _create(stack, chat_id("u-rb"))
    assert (await stack.chat.summary(SP, "bob")).unread == 1
