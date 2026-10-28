"""What a person chose decides what they hear (notifications redesign).

* **D4** — every type falls under one of seven kinds; each kind's choice
  (theirs, else its default) decides: Push → a row and a push, In app → a
  row and no push, Off → nothing. Needs you can't be Off; Messages and
  calls Off stops chat pushes.
* **D9** — a space muted "for a while" is quiet until then, and back at the
  level from before afterwards.
* **D10** — a muted post makes no rows; activity keeps the mute alive, and
  it lapses after 90 quiet days.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import orjson
import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.conversation import (
    Conversation,
    ConversationMember,
    ConversationMessage,
    ConversationType,
)
from socialhome.domain.events import (
    CommentAdded,
    DmMessageCreated,
    PostCreated,
    PostReactionChanged,
    SpaceJoinRequested,
    SpacePostCreated,
)
from socialhome.domain.mention import Mention, MentionType
from socialhome.domain.notification_kinds import KNOWN_TYPES
from socialhome.domain.post import Comment, CommentType, Post, PostType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.conversation_repo import SqliteConversationRepo
from socialhome.repositories.notification_repo import SqliteNotificationRepo
from socialhome.repositories.post_repo import SqlitePostRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.notification_service import NotificationService
from socialhome.services.user_service import UserService
from tests.services.test_notification_i18n import _created_types


class _Push:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def push_to_user(self, user_id, payload) -> None:
        self.sent.append((user_id, payload))

    def to(self, user) -> list[str]:
        return [p.title for uid, p in self.sent if uid == user.user_id]


@pytest.fixture
async def s(tmp_dir):
    kp = generate_identity_keypair()
    db = AsyncDatabase(tmp_dir / "gate.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (
            derive_instance_id(kp.public_key),
            kp.private_key.hex(),
            kp.public_key.hex(),
            "aa" * 32,
        ),
    )
    bus = EventBus()
    users = SqliteUserRepo(db)
    spaces = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x0c" * 32))
    notifs = SqliteNotificationRepo(db, max_per_user=200)
    convos = SqliteConversationRepo(db)
    svc = NotificationService(notifs, users, spaces, bus, conversation_repo=convos)
    posts = SqlitePostRepo(db)
    svc.attach_post_repos(posts, SqliteSpacePostRepo(db))
    push = _Push()
    svc.attach_push_service(push)
    svc.wire()
    user_svc = UserService(users, bus, own_instance_public_key=kp.public_key)

    class S:
        pass

    st = S()
    st.db, st.bus, st.notifs, st.push, st.posts, st.convos = (
        db,
        bus,
        notifs,
        push,
        posts,
        convos,
    )
    st.users, st.user_svc = users, user_svc

    async def user(name: str, **prefs):
        u = await user_svc.provision(username=name, display_name=name.title())
        if prefs:
            await user_svc.patch_preferences(name, prefs)
        return u

    st.user = user
    yield st
    await db.shutdown()


async def _types(s, user) -> list[str]:
    return [n.type for n in await s.notifs.list(user.user_id, limit=50)]


def _post(author, pid: str = "p1") -> Post:
    return Post(
        id=pid,
        author=author.user_id,
        type=PostType.TEXT,
        content="secret",
        created_at=datetime.now(UTC),
    )


async def _comment(s, author, cid: str, post_id: str = "p1") -> None:
    c = Comment(
        id=cid,
        post_id=post_id,
        author=author.user_id,
        type=CommentType.TEXT,
        created_at=datetime.now(UTC),
        content="secret",
    )
    await s.posts.add_comment(c)
    await s.bus.publish(CommentAdded(post_id=post_id, comment=c))


async def _space(s, sid: str, members: dict, space_type: str = "public") -> None:
    await s.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, space_type) VALUES(?, ?, 'inst', 'owner', ?, ?)",
        (sid, sid.title(), "ab" * 32, space_type),
    )
    for u, role in members.items():
        await s.db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
            (sid, u.user_id, role),
        )


def _mention(user) -> Mention:
    return Mention(type=MentionType.USER, raw=f"@{user.username}", user_id=user.user_id)


# ── D4: every type has a kind ─────────────────────────────────────────


def test_d4_every_type_the_service_creates_has_a_kind():
    assert _created_types() - KNOWN_TYPES == set()


# ── D4: the kind's choice decides ─────────────────────────────────────


async def test_d4_defaults_reactions_in_app_replies_push(s):
    anna, bob = await s.user("anna"), await s.user("bob")
    await s.posts.save(_post(bob))
    await s.bus.publish(
        PostReactionChanged(post=_post(bob), reactor_user_id=anna.user_id, emoji="❤️")
    )
    await _comment(s, anna, "c1")
    assert sorted(await _types(s, bob)) == ["post_reacted", "post_replied"]
    assert s.push.to(bob) == ["Anna commented on your post"]


async def test_d4_reactions_set_to_push_push(s):
    anna = await s.user("anna")
    bob = await s.user("bob", notif_kinds={"reactions": "push"})
    await s.posts.save(_post(bob))
    await s.bus.publish(
        PostReactionChanged(post=_post(bob), reactor_user_id=anna.user_id, emoji="❤️")
    )
    assert await _types(s, bob) == ["post_reacted"]
    assert len(s.push.to(bob)) == 1


async def test_d4_replies_in_app_is_a_row_without_a_push(s):
    anna = await s.user("anna")
    bob = await s.user("bob", notif_kinds={"replies": "in_app"})
    await s.posts.save(_post(bob))
    await _comment(s, anna, "c1")
    assert await _types(s, bob) == ["post_replied"]
    assert s.push.to(bob) == []


async def test_d4_replies_off_is_nothing(s):
    anna = await s.user("anna")
    bob = await s.user("bob", notif_kinds={"replies": "off"})
    await s.posts.save(_post(bob))
    await _comment(s, anna, "c1")
    assert await _types(s, bob) == []
    assert s.push.to(bob) == []


async def test_d4_activity_off_drops_the_household_feed_row(s):
    anna = await s.user("anna")
    bob = await s.user("bob", notif_kinds={"activity": "off"})
    cara = await s.user("cara")
    await s.bus.publish(PostCreated(post=_post(anna)))
    assert await _types(s, bob) == []
    assert await _types(s, cara) == ["post_created"]
    assert s.push.to(cara) == []  # Activity: In app by default


async def test_d4_activity_push_pushes_a_new_space_post(s):
    anna = await s.user("anna")
    bob = await s.user("bob", notif_kinds={"activity": "push"})
    await _space(s, "fam", {anna: "owner", bob: "member"}, space_type="private")
    await s.bus.publish(
        SpacePostCreated(post=_post(anna, "sp1"), space_id="fam", mentions=())
    )
    assert await _types(s, bob) == ["space_post_created"]
    assert len(s.push.to(bob)) == 1


async def test_d4_needs_you_can_not_be_turned_off(s):
    """A blob that says Off for Needs you (hand-edited, or an old client)
    still pushes: only Push and In app exist for it."""
    joiner = await s.user("joiner")
    admin = await s.user("ada", notif_kinds={"needs_you": "off"})
    quiet = await s.user("quiet", notif_kinds={"needs_you": "in_app"})
    await _space(s, "club", {admin: "owner", quiet: "admin"})
    await s.bus.publish(
        SpaceJoinRequested(space_id="club", user_id=joiner.user_id, request_id="r1")
    )
    assert await _types(s, admin) == ["space_join_requested"]
    assert len(s.push.to(admin)) == 1
    assert await _types(s, quiet) == ["space_join_requested"]
    assert s.push.to(quiet) == []


async def _chat(s, *users) -> None:
    await s.convos.create(
        Conversation(
            id="g1",
            type=ConversationType.GROUP_DM,
            created_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    for u in users:
        await s.convos.add_member(
            ConversationMember(
                conversation_id="g1",
                username=u.username,
                joined_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            )
        )


async def _message(s, sender, recipients, mid, mentions=()) -> None:
    await s.convos.save_message(
        ConversationMessage(
            id=mid,
            conversation_id="g1",
            sender_user_id=sender.user_id,
            content="secret",
            created_at=datetime.now(UTC),
        )
    )
    await s.bus.publish(
        DmMessageCreated(
            conversation_id="g1",
            message_id=mid,
            sender_user_id=sender.user_id,
            sender_display_name=sender.display_name,
            recipient_user_ids=tuple(r.user_id for r in recipients),
            content="secret",
            mentions=tuple(mentions),
        )
    )


async def test_d4_messages_off_stops_chat_pushes_but_not_mentions(s):
    anna = await s.user("anna")
    bob = await s.user("bob", notif_kinds={"messages": "off"})
    await _chat(s, anna, bob)
    await _message(s, anna, [bob], "m1")
    assert s.push.to(bob) == []
    await _message(s, anna, [bob], "m2", mentions=[_mention(bob)])
    assert await _types(s, bob) == ["dm_mention"]  # Replies and mentions
    assert len(s.push.to(bob)) == 1


# ── D9: a space quiet for a while ─────────────────────────────────────


async def test_d9_a_timed_mute_is_quiet_then_back_to_the_level_before(s):
    anna, bob = await s.user("anna"), await s.user("bob")
    await _space(s, "club", {anna: "owner", bob: "member"})
    soon = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await s.notifs.set_space_notif_level(
        user_id=bob.user_id, space_id="club", level="mentions", muted_until=soon
    )
    await s.bus.publish(
        SpacePostCreated(
            post=_post(anna, "sp1"), space_id="club", mentions=(_mention(bob),)
        )
    )
    assert await _types(s, bob) == []
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await s.notifs.set_space_notif_level(
        user_id=bob.user_id, space_id="club", level="mentions", muted_until=past
    )
    await s.bus.publish(
        SpacePostCreated(
            post=_post(anna, "sp2"), space_id="club", mentions=(_mention(bob),)
        )
    )
    assert await _types(s, bob) == ["space_mention"]


# ── D10: a muted post ─────────────────────────────────────────────────


async def test_d10_a_muted_post_makes_no_rows_and_stays_muted_while_active(s):
    anna = await s.user("anna")
    two_days = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    bob = await s.user("bob", notif_muted_posts={"post:p1": two_days})
    await s.posts.save(_post(bob))
    await s.posts.save(_post(bob, "p2"))
    await _comment(s, anna, "c1")
    assert await _types(s, bob) == []
    assert s.push.to(bob) == []
    stored = orjson.loads((await s.users.get("bob")).preferences_json)
    moved = datetime.fromisoformat(stored["notif_muted_posts"]["post:p1"])
    assert datetime.now(UTC) - moved < timedelta(minutes=1)
    # Another post is not muted.
    await _comment(s, anna, "c2", post_id="p2")
    assert await _types(s, bob) == ["post_replied"]


async def test_d10_a_mute_lapses_after_90_quiet_days(s):
    anna = await s.user("anna")
    stale = (datetime.now(UTC) - timedelta(days=91)).isoformat()
    bob = await s.user("bob", notif_muted_posts={"post:p1": stale})
    await s.posts.save(_post(bob))
    await _comment(s, anna, "c1")
    assert await _types(s, bob) == ["post_replied"]
