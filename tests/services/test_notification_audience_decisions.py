"""You hear about replies, not every comment (notifications redesign).

One test group per owner decision:

* **D3** — a chat message pushes but never makes a Notifications row; a
  mention in a chat does.
* **D5** — the default level by space type: household / private spaces are
  "Everything" (in-app rows, no push for plain posts and comments), public /
  global / followed spaces are "Replies and mentions". A level someone chose
  wins.
* **D6** — a comment pushes the post's author, the author of the comment it
  answers and anyone it mentions; others who commented and members at
  "Everything" get it in the app only; nobody else hears.
* **D8** — the event that starts a row alerts, repeats on a row still
  unread are silent; a chat alerts again after five quiet minutes.
* **D13** — a reaction on a post is an in-app row grouped per post, never
  a push.
* ``@here`` never reaches a follower; someone adding an event to your
  personal calendar pushes you (Plans).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.calendar import Calendar, CalendarEvent
from socialhome.domain.conversation import (
    Conversation,
    ConversationMember,
    ConversationMessage,
    ConversationType,
)
from socialhome.domain.events import (
    CalendarEventCreated,
    CommentAdded,
    DmMessageCreated,
    NotificationCreated,
    PostCreated,
    PostReactionChanged,
    SpacePostCreated,
)
from socialhome.domain.mention import Mention, MentionType
from socialhome.domain.notification import NotificationSubject
from socialhome.domain.post import Comment, CommentType, Post, PostType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.calendar_repo import SqliteCalendarRepo
from socialhome.repositories.conversation_repo import SqliteConversationRepo
from socialhome.repositories.notification_repo import (
    SqliteNotificationRepo,
    new_notification,
)
from socialhome.repositories.post_repo import SqlitePostRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.notification_service import (
    CHAT_REALERT_SECONDS,
    NotificationService,
)
from socialhome.services.user_service import UserService


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
    db = AsyncDatabase(tmp_dir / "decisions.db", batch_timeout_ms=10)
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
    posts, space_posts = SqlitePostRepo(db), SqliteSpacePostRepo(db)
    svc.attach_post_repos(posts, space_posts)
    calendars = SqliteCalendarRepo(db)
    svc.attach_personal_calendar_repo(calendars)
    push = _Push()
    svc.attach_push_service(push)
    svc.wire()

    class S:
        pass

    st = S()
    st.db, st.bus, st.svc, st.notifs, st.push = db, bus, svc, notifs, push
    st.posts, st.space_posts, st.convos, st.calendars = (
        posts,
        space_posts,
        convos,
        calendars,
    )
    st.created = []

    async def _nc(ev: NotificationCreated) -> None:
        st.created.append(ev)

    bus.subscribe(NotificationCreated, _nc)
    user_svc = UserService(users, bus, own_instance_public_key=kp.public_key)

    async def user(name: str):
        return await user_svc.provision(username=name, display_name=name.title())

    st.user = user
    yield st
    await db.shutdown()


# ── helpers ───────────────────────────────────────────────────────────


async def _space(s, sid: str, space_type: str, members: dict) -> str:
    """Space *sid* of *space_type*; *members* maps user → role."""
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
    return sid


def _post(author, pid: str = "p1") -> Post:
    return Post(
        id=pid,
        author=author.user_id,
        type=PostType.TEXT,
        content="secret",
        created_at=datetime.now(UTC),
    )


def _comment(author, cid: str, *, post_id: str = "p1", parent: str | None = None):
    return Comment(
        id=cid,
        post_id=post_id,
        author=author.user_id,
        type=CommentType.TEXT,
        created_at=datetime.now(UTC),
        content="secret",
        parent_id=parent,
    )


async def _space_comment(s, space_id, author, cid, *, parent=None, mentions=()):
    """Store a space comment and publish it, as the service does."""
    c = _comment(author, cid, parent=parent)
    assert await s.space_posts.add_comment(c, space_id=space_id)
    await s.bus.publish(
        CommentAdded(post_id="p1", comment=c, space_id=space_id, mentions=mentions)
    )


async def _household_comment(s, author, cid, *, parent=None):
    c = _comment(author, cid, parent=parent)
    await s.posts.add_comment(c)
    await s.bus.publish(CommentAdded(post_id="p1", comment=c))


async def _rows(s, user) -> list[tuple[str, str]]:
    return [(n.type, n.title) for n in await s.notifs.list(user.user_id, limit=50)]


def _mention(user) -> Mention:
    return Mention(type=MentionType.USER, raw=f"@{user.username}", user_id=user.user_id)


# ── D3: chat messages are not Notifications rows ──────────────────────


async def _group_chat(s, conv_id: str, *users) -> None:
    await s.convos.create(
        Conversation(id=conv_id, type=ConversationType.GROUP_DM, created_at=_ago(3600))
    )
    for u in users:
        await s.convos.add_member(
            ConversationMember(
                conversation_id=conv_id,
                username=u.username,
                joined_at=_ago(3600).isoformat(),
            )
        )


def _ago(seconds: float) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


async def _message(s, conv_id, sender, recipients, mid, *, ago=0.0, mentions=()):
    """Store message *mid* (sent *ago* seconds back) and publish it."""
    await s.convos.save_message(
        ConversationMessage(
            id=mid,
            conversation_id=conv_id,
            sender_user_id=sender.user_id,
            content="secret",
            created_at=_ago(ago),
        )
    )
    await s.bus.publish(
        DmMessageCreated(
            conversation_id=conv_id,
            message_id=mid,
            sender_user_id=sender.user_id,
            sender_display_name=sender.display_name,
            recipient_user_ids=tuple(r.user_id for r in recipients),
            content="secret",
            mentions=tuple(mentions),
        )
    )


async def test_d3_chat_message_pushes_but_makes_no_row(s):
    anna, bob = await s.user("anna"), await s.user("bob")
    await _group_chat(s, "g1", anna, bob)
    await _message(s, "g1", anna, [bob], "m1")
    assert await _rows(s, bob) == []
    assert await s.notifs.count_unread(bob.user_id) == 0
    assert s.created == []  # no row, so no Home Assistant notification event
    assert s.push.to(bob) == ["Anna messaged you"]


async def test_d3_a_mention_in_a_chat_still_makes_a_row(s):
    anna, bob = await s.user("anna"), await s.user("bob")
    await _group_chat(s, "g1", anna, bob)
    await _message(s, "g1", anna, [bob], "m1", mentions=[_mention(bob)])
    assert [t for t, _ in await _rows(s, bob)] == ["dm_mention"]
    assert len(s.push.to(bob)) == 1


# ── D8: alert once per thing, chats again after five quiet minutes ────


async def test_d8_chat_burst_alerts_once_then_again_after_five_quiet_minutes(s):
    anna, bob = await s.user("anna"), await s.user("bob")
    await _group_chat(s, "g1", anna, bob)
    # A burst: the first unread message alerts, the rest are quiet.
    await _message(s, "g1", anna, [bob], "m1", ago=CHAT_REALERT_SECONDS + 120)
    await _message(s, "g1", anna, [bob], "m2", ago=CHAT_REALERT_SECONDS + 60)
    await _message(s, "g1", anna, [bob], "m3", ago=CHAT_REALERT_SECONDS + 30)
    assert len(s.push.to(bob)) == 1
    # Still unread, but the chat went quiet for five minutes: alert again.
    await _message(s, "g1", anna, [bob], "m4")
    assert len(s.push.to(bob)) == 2
    # …and the next one right after it is quiet again.
    await _message(s, "g1", anna, [bob], "m5")
    assert len(s.push.to(bob)) == 2


async def test_d8_a_chat_read_in_between_alerts_on_the_next_message(s):
    anna, bob = await s.user("anna"), await s.user("bob")
    await _group_chat(s, "g1", anna, bob)
    await _message(s, "g1", anna, [bob], "m1", ago=20)
    await _message(s, "g1", anna, [bob], "m2", ago=10)
    assert len(s.push.to(bob)) == 1
    await s.convos.set_last_read("g1", bob.username, at=_ago(5).isoformat())
    await _message(s, "g1", anna, [bob], "m3")
    assert len(s.push.to(bob)) == 2


async def _save(s, user, *, ntype: str, key: str):
    return await s.svc._save_notif(
        new_notification(user_id=user.user_id, type=ntype, title=ntype, link_url="/x"),
        about=NotificationSubject(key),
        actor_user_id=None,
    )


async def test_d8_a_grouped_row_alerts_on_its_first_event_then_stays_silent(s):
    bob = await s.user("bob")
    for _ in range(3):
        await _save(s, bob, ntype="space_join_requested", key="space:s1")
    assert len(s.push.to(bob)) == 1
    assert [e.silent for e in s.created] == [False, True, True]
    # Read it: the next event re-opens the row and alerts again.
    [row] = await s.notifs.list(bob.user_id)
    await s.notifs.mark_read(row.id, bob.user_id)
    await _save(s, bob, ntype="space_join_requested", key="space:s1")
    assert len(s.push.to(bob)) == 2


async def test_d8_a_quiet_row_does_not_realert_unless_it_is_a_chat(s):
    """Five quiet minutes re-alert a chat row (``dm_mention``), never any
    other unread row."""
    bob = await s.user("bob")
    for ntype, key in (("dm_mention", "dm:g1"), ("post_replied", "post:p1")):
        await _save(s, bob, ntype=ntype, key=key)
        [row] = [n for n in await s.notifs.list(bob.user_id) if n.type == ntype]
        await s.db.enqueue(
            "UPDATE notifications SET created_at=? WHERE id=?",
            (_ago(CHAT_REALERT_SECONDS + 1).isoformat(), row.id),
        )
        await _save(s, bob, ntype=ntype, key=key)
    assert [p.title for _, p in s.push.sent] == [
        "dm_mention",
        "dm_mention",
        "post_replied",
    ]


# ── D5: defaults by space type ────────────────────────────────────────


async def test_d5_household_feed_post_is_an_in_app_row_without_push(s):
    anna, bob = await s.user("anna"), await s.user("bob")
    await s.bus.publish(PostCreated(post=_post(anna)))
    assert [t for t, _ in await _rows(s, bob)] == ["post_created"]
    assert s.push.sent == []


@pytest.mark.parametrize("space_type", ["household", "private"])
async def test_d5_household_and_private_spaces_default_to_everything(s, space_type):
    anna, bob = await s.user("anna"), await s.user("bob")
    sid = await _space(s, "fam", space_type, {anna: "owner", bob: "member"})
    await s.bus.publish(SpacePostCreated(post=_post(anna), space_id=sid))
    assert [t for t, _ in await _rows(s, bob)] == ["space_post_created"]
    assert s.push.sent == []  # Everything is in the app, never a buzz


@pytest.mark.parametrize(
    ("space_type", "role"),
    [("public", "member"), ("global", "member"), ("private", "subscriber")],
)
async def test_d5_public_global_and_followed_default_to_replies_and_mentions(
    s, space_type, role
):
    anna, bob = await s.user("anna"), await s.user("bob")
    sid = await _space(s, "pub", space_type, {anna: "owner", bob: role})
    await s.bus.publish(SpacePostCreated(post=_post(anna), space_id=sid))
    assert await _rows(s, bob) == []
    await s.bus.publish(
        SpacePostCreated(
            post=_post(anna, "p2"), space_id=sid, mentions=(_mention(bob),)
        )
    )
    assert [t for t, _ in await _rows(s, bob)] == ["space_mention"]
    assert s.push.to(bob) == ["Anna mentioned you in Pub"]


async def test_d5_a_level_someone_chose_wins_over_the_default(s):
    anna, bob, cleo = await s.user("anna"), await s.user("bob"), await s.user("cleo")
    sid = await _space(
        s, "pub", "public", {anna: "owner", bob: "member", cleo: "member"}
    )
    fam = await _space(s, "fam", "private", {anna: "owner", cleo: "member"})
    await s.notifs.set_space_notif_level(user_id=bob.user_id, space_id=sid, level="all")
    await s.notifs.set_space_notif_level(
        user_id=cleo.user_id, space_id=fam, level="mentions"
    )
    await s.bus.publish(SpacePostCreated(post=_post(anna), space_id=sid))
    await s.bus.publish(SpacePostCreated(post=_post(anna, "p2"), space_id=fam))
    assert [t for t, _ in await _rows(s, bob)] == ["space_post_created"]
    assert await _rows(s, cleo) == []


async def test_d5_a_space_event_is_activity_at_everything_only(s):
    anna, bob, cleo = await s.user("anna"), await s.user("bob"), await s.user("cleo")
    sid = await _space(s, "fam", "private", {anna: "owner", bob: "member"})
    pub = await _space(s, "pub", "public", {anna: "owner", cleo: "member"})
    start = datetime(2026, 5, 1, 10, tzinfo=UTC)
    for cal, eid in ((sid, "e1"), (pub, "e2")):
        await s.bus.publish(
            CalendarEventCreated(
                event=CalendarEvent(
                    id=eid,
                    calendar_id=cal,
                    summary="Dinner",
                    created_by=anna.user_id,
                    start=start,
                    end=start + timedelta(hours=1),
                )
            )
        )
    assert [t for t, _ in await _rows(s, bob)] == ["calendar_event_created"]
    assert await _rows(s, cleo) == []
    assert s.push.sent == []


# ── D6: who hears about a comment ─────────────────────────────────────


async def _public_thread(s):
    """A public space: Anna's post p1, Ben commented, Cleo and Dora are
    members at the default ("Replies and mentions")."""
    anna, ben, cleo, dora = (
        await s.user("anna"),
        await s.user("ben"),
        await s.user("cleo"),
        await s.user("dora"),
    )
    sid = await _space(
        s,
        "pub",
        "public",
        {anna: "owner", ben: "member", cleo: "member", dora: "member"},
    )
    await s.space_posts.save(sid, _post(anna))
    await s.space_posts.add_comment(_comment(ben, "c1"), space_id=sid)
    return sid, anna, ben, cleo, dora


async def test_d6_a_comment_pushes_the_post_author(s):
    sid, anna, _ben, cleo, _dora = await _public_thread(s)
    await _space_comment(s, sid, cleo, "c2")
    assert await _rows(s, anna) == [
        ("post_replied", "Cleo commented on your post in Pub")
    ]
    assert s.push.to(anna) == ["Cleo commented on your post in Pub"]


async def test_d6_a_reply_pushes_the_author_of_the_comment_it_answers(s):
    sid, anna, ben, cleo, _dora = await _public_thread(s)
    await _space_comment(s, sid, cleo, "c2", parent="c1")
    assert await _rows(s, ben) == [
        ("comment_replied", "Cleo replied to your comment in Pub")
    ]
    assert s.push.to(ben) == ["Cleo replied to your comment in Pub"]
    # The post's author hears it as a comment on their post.
    assert [t for t, _ in await _rows(s, anna)] == ["post_replied"]


async def test_d6_a_comment_pushes_anyone_it_mentions(s):
    sid, _anna, _ben, cleo, dora = await _public_thread(s)
    await _space_comment(s, sid, cleo, "c2", mentions=(_mention(dora),))
    assert [t for t, _ in await _rows(s, dora)] == ["space_mention"]
    assert len(s.push.to(dora)) == 1


async def test_d6_others_who_commented_hear_in_the_app_only(s):
    sid, _anna, ben, cleo, _dora = await _public_thread(s)
    await _space_comment(s, sid, cleo, "c2")  # not a reply to Ben
    assert [t for t, _ in await _rows(s, ben)] == ["space_comment_added"]
    assert s.push.to(ben) == []


async def test_d6_nobody_else_hears(s):
    sid, _anna, _ben, cleo, dora = await _public_thread(s)
    await _space_comment(s, sid, cleo, "c2")
    assert await _rows(s, dora) == []
    assert s.push.to(dora) == []


async def test_d6_members_at_everything_hear_in_the_app_only(s):
    sid, _anna, _ben, cleo, dora = await _public_thread(s)
    await s.notifs.set_space_notif_level(
        user_id=dora.user_id, space_id=sid, level="all"
    )
    await _space_comment(s, sid, cleo, "c2")
    assert [t for t, _ in await _rows(s, dora)] == ["space_comment_added"]
    assert s.push.to(dora) == []


async def test_d6_muted_hears_nothing_not_even_a_reply(s):
    sid, anna, _ben, cleo, _dora = await _public_thread(s)
    await s.notifs.set_space_notif_level(
        user_id=anna.user_id, space_id=sid, level="muted"
    )
    await _space_comment(s, sid, cleo, "c2")
    assert await _rows(s, anna) == []


async def test_d6_household_comment_pushes_only_the_people_it_is_for(s):
    """The household feed is "Everything": everyone gets the comment in the
    app; the post's author and the replied-to commenter also get a push."""
    anna, ben, cleo, dora = (
        await s.user("anna"),
        await s.user("ben"),
        await s.user("cleo"),
        await s.user("dora"),
    )
    await s.posts.save(_post(anna))
    await _household_comment(s, ben, "c1")
    await s.notifs.mark_all_read(anna.user_id)  # D8: else c2 is a quiet repeat
    s.push.sent.clear()
    await _household_comment(s, cleo, "c2", parent="c1")
    assert ("post_replied", "Cleo commented on your post") in await _rows(s, anna)
    assert ("comment_replied", "Cleo replied to your comment") in await _rows(s, ben)
    assert [t for t, _ in await _rows(s, dora)] == ["comment_added"]
    assert sorted(uid for uid, _ in s.push.sent) == sorted([anna.user_id, ben.user_id])


async def test_d6_comments_on_your_post_buzz_once_until_read(s):
    """D6 + D8: three comments on Anna's post are one row and one push."""
    sid, anna, ben, cleo, dora = await _public_thread(s)
    for author, cid in ((cleo, "c2"), (dora, "c3"), (ben, "c4")):
        await _space_comment(s, sid, author, cid)
    rows = await s.notifs.list(anna.user_id)
    assert [(n.type, n.params.new) for n in rows] == [("post_replied", 3)]
    assert len(s.push.to(anna)) == 1


# ── @here never reaches a follower ────────────────────────────────────


async def test_here_never_reaches_a_follower(s):
    anna, bob, fern = await s.user("anna"), await s.user("bob"), await s.user("fern")
    sid = await _space(
        s, "pub", "public", {anna: "owner", bob: "member", fern: "subscriber"}
    )
    # Even a follower who chose "Everything" gets the post, never the page.
    await s.notifs.set_space_notif_level(
        user_id=fern.user_id, space_id=sid, level="all"
    )
    await s.bus.publish(
        SpacePostCreated(
            post=_post(anna),
            space_id=sid,
            mentions=(Mention(type=MentionType.HERE, raw="@here", user_id=None),),
        )
    )
    assert [t for t, _ in await _rows(s, bob)] == ["space_here"]
    assert [t for t, _ in await _rows(s, fern)] == ["space_post_created"]
    assert s.push.to(fern) == []


# ── D13: reactions ────────────────────────────────────────────────────


def _reaction(post, reactor, *, space_id=None, emoji="🎉", added=True):
    return PostReactionChanged(
        post=post,
        space_id=space_id,
        reactor_user_id=reactor.user_id,
        emoji=emoji,
        added=added,
    )


async def test_d13_reactions_on_a_household_post_group_in_app_without_push(s):
    anna, ben, cleo = await s.user("anna"), await s.user("ben"), await s.user("cleo")
    post = _post(anna)
    await s.bus.publish(_reaction(post, ben))
    await s.bus.publish(_reaction(post, cleo, emoji="❤️"))
    [row] = await s.notifs.list(anna.user_id)
    assert row.type == "post_reacted"
    assert row.object_key == "post:p1"
    assert [a.name for a in row.params.actors] == ["Cleo", "Ben"]
    assert row.link_url == "/post/p1"
    assert s.push.sent == []


async def test_d13_a_reaction_on_a_space_post_names_the_space(s):
    anna, ben = await s.user("anna"), await s.user("ben")
    sid = await _space(s, "fam", "private", {anna: "owner", ben: "member"})
    await s.bus.publish(_reaction(_post(anna), ben, space_id=sid))
    rows = await s.notifs.list(anna.user_id)
    assert [(n.type, n.title, n.object_key) for n in rows] == [
        ("post_reacted", "Ben reacted 🎉 to your post in Fam", "space_post:p1")
    ]
    assert s.push.sent == []


async def test_d13_removed_and_own_reactions_stay_silent(s):
    anna, ben = await s.user("anna"), await s.user("ben")
    sid = await _space(s, "fam", "private", {ben: "member"})
    await s.bus.publish(_reaction(_post(anna), ben, added=False))
    await s.bus.publish(_reaction(_post(anna), anna))
    # A space post whose author no longer holds a seat there.
    await s.bus.publish(_reaction(_post(anna, "p2"), ben, space_id=sid))
    assert await s.notifs.list(anna.user_id) == []


# ── Plans: an event added to your personal calendar ───────────────────


async def test_an_event_added_to_your_personal_calendar_pushes_you(s):
    anna, bob = await s.user("anna"), await s.user("bob")
    await s.calendars.save_calendar(
        Calendar(
            id="cal-bob",
            name="Bob",
            color="#4A90E2",
            owner_username=bob.username,
            calendar_type="personal",
        )
    )
    start = datetime(2026, 5, 1, 10, tzinfo=UTC)
    await s.bus.publish(
        CalendarEventCreated(
            event=CalendarEvent(
                id="e1",
                calendar_id="cal-bob",
                summary="Dentist",
                created_by=anna.user_id,
                start=start,
                end=start + timedelta(hours=1),
            )
        )
    )
    assert [t for t, _ in await _rows(s, bob)] == ["calendar_event_created"]
    assert s.push.to(bob) == ["Anna added an event to your calendar"]
