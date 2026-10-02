"""Tests for socialhome.services.notification_service."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.crypto import generate_identity_keypair, derive_instance_id
from socialhome.db.database import AsyncDatabase
from socialhome.domain.conversation import (
    MUTED_FOREVER,
    Conversation,
    ConversationMember,
    ConversationType,
)
from socialhome.domain.events import (
    CommentAdded,
    CommentUpdated,
    DmMessageCreated,
    DmMessageUpdated,
    PostEdited,
    SpacePostCreated,
)
from socialhome.domain.mention import Mention, MentionType
from socialhome.domain.post import Comment, CommentType, Post, PostType
from socialhome.domain.space import SpaceFeatureAccess, SpaceFeatures
from socialhome.domain.task import Task, TaskStatus
from socialhome.domain.user import RemoteUser
from socialhome.domain.events import TaskAssigned
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.calendar_repo import SqliteCalendarRepo
from socialhome.repositories.conversation_repo import SqliteConversationRepo
from socialhome.repositories.notification_repo import SqliteNotificationRepo
from socialhome.repositories.post_repo import SqlitePostRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.services.feed_service import FeedService
from socialhome.services.notification_service import NotificationService
from socialhome.services.space_service import SpaceService
from socialhome.services.user_service import UserService


def _space_repo(db):
    """Test space repo wired with a fixed KEK for at-rest seed wrapping."""
    return SqliteSpaceRepo(db, key_manager=KeyManager(b"\x0c" * 32))


@pytest.fixture
async def stack(tmp_dir):
    """Full service stack for notification service tests."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        """INSERT INTO instance_identity(instance_id, identity_private_key,
           identity_public_key, routing_secret) VALUES(?,?,?,?)""",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    user_repo = SqliteUserRepo(db)
    post_repo = SqlitePostRepo(db)
    space_repo = _space_repo(db)
    notif_repo = SqliteNotificationRepo(db, max_per_user=50)
    calendar_repo = SqliteCalendarRepo(db)
    user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)
    feed_svc = FeedService(post_repo, user_repo, bus)
    conv_repo = SqliteConversationRepo(db)
    notif_svc = NotificationService(
        notif_repo, user_repo, space_repo, bus, conversation_repo=conv_repo
    )
    notif_svc.attach_personal_calendar_repo(calendar_repo)
    notif_svc.wire()

    class Stack:
        pass

    s = Stack()
    s.db = db
    s.user_svc = user_svc
    s.feed_svc = feed_svc
    s.notif_svc = notif_svc
    s.notif_repo = notif_repo
    s.space_repo = space_repo
    s.calendar_repo = calendar_repo
    s.conv_repo = conv_repo
    s.bus = bus

    async def provision_user(username, **kw):
        return await user_svc.provision(username=username, display_name=username, **kw)

    s.provision_user = provision_user
    yield s
    await db.shutdown()


async def test_post_created_notifies_others(stack):
    """Creating a feed post sends a notification to other users, not the author."""
    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    await stack.feed_svc.create_post(
        author_user_id=a.user_id,
        type=PostType.TEXT,
        content="hi",
    )
    bob_n = await stack.notif_repo.list(b.user_id, limit=10)
    anna_n = await stack.notif_repo.list(a.user_id, limit=10)
    assert len(bob_n) >= 1
    assert len(anna_n) == 0


async def test_task_assigned(stack):
    """TaskAssigned event generates a notification for the assignee."""
    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    now = datetime.now(timezone.utc)
    evt = TaskAssigned(
        task=Task(
            id="t1",
            list_id="l1",
            title="Buy milk",
            status=TaskStatus.TODO,
            position=0,
            created_by=a.user_id,
            created_at=now,
            updated_at=now,
        ),
        assigned_to=b.user_id,
    )
    await stack.bus.publish(evt)
    bob_n = await stack.notif_repo.list(b.user_id, limit=10)
    assert any("Buy milk" in n.title for n in bob_n)


async def test_self_assign_no_notification(stack):
    """Assigning a task to yourself does not generate a notification."""
    a = await stack.provision_user("anna")
    now = datetime.now(timezone.utc)
    evt = TaskAssigned(
        task=Task(
            id="t1",
            list_id="l1",
            title="Self",
            status=TaskStatus.TODO,
            position=0,
            created_by=a.user_id,
            created_at=now,
            updated_at=now,
        ),
        assigned_to=a.user_id,
    )
    pre = len(await stack.notif_repo.list(a.user_id, limit=50))
    await stack.bus.publish(evt)
    post = len(await stack.notif_repo.list(a.user_id, limit=50))
    assert post == pre


async def test_comment_notifies_others(stack):
    """CommentAdded notifies all household members except the commenter."""
    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    c = await stack.provision_user("carl")
    post = await stack.feed_svc.create_post(
        author_user_id=a.user_id, type=PostType.TEXT, content="hi"
    )
    # Clear notifications from post creation
    for u in [a, b, c]:
        await stack.notif_repo.mark_all_read(u.user_id)
    # Bob comments
    await stack.feed_svc.add_comment(post.id, author_user_id=b.user_id, content="nice")
    # Anna and Carl should get a comment notification, not Bob
    anna_n = await stack.notif_repo.list(a.user_id, limit=50)
    carl_n = await stack.notif_repo.list(c.user_id, limit=50)
    bob_n = await stack.notif_repo.list(b.user_id, limit=50)
    assert any("commented" in n.title for n in anna_n)
    assert any("commented" in n.title for n in carl_n)
    assert not any("commented" in n.title and n.read_at is None for n in bob_n)


async def test_space_post_notifies_members(stack):
    """SpacePostCreated notifies space members except the author."""
    from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
    from socialhome.services.space_service import SpaceService

    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space_repo = _space_repo(stack.db)
    spost_repo = SqliteSpacePostRepo(stack.db)
    space_svc = SpaceService(
        space_repo,
        spost_repo,
        SqliteUserRepo(stack.db),
        stack.bus,
        own_instance_id="iid",
    )
    space = await space_svc.create_space(owner_username="anna", name="S")
    await space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    await space_svc.create_post(
        space.id, author_user_id=a.user_id, type=PostType.TEXT, content="space hello"
    )
    bob_n = await stack.notif_repo.list(b.user_id, limit=50)
    assert any("posted in S" in n.title for n in bob_n)


async def test_space_post_respects_muted_notif_pref(stack):
    """Members with level='muted' receive no space-post notification."""
    from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
    from socialhome.services.space_service import SpaceService

    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space_repo = _space_repo(stack.db)
    spost_repo = SqliteSpacePostRepo(stack.db)
    space_svc = SpaceService(
        space_repo,
        spost_repo,
        SqliteUserRepo(stack.db),
        stack.bus,
        own_instance_id="iid",
    )
    space = await space_svc.create_space(owner_username="anna", name="Quiet")
    await space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    await stack.notif_repo.set_space_notif_level(
        user_id=b.user_id,
        space_id=space.id,
        level="muted",
    )
    await space_svc.create_post(
        space.id, author_user_id=a.user_id, type=PostType.TEXT, content="shhh"
    )
    bob_n = await stack.notif_repo.list(b.user_id, limit=50)
    assert not any("posted in Quiet" in n.title for n in bob_n)


async def _mention_space(stack, *members, name="M", levels=None):
    """Space owned by ``anna`` with *members* seated; returns
    ``(space_svc, space, users_by_name)``. ``levels`` maps username →
    notif level."""

    users = {"anna": await stack.provision_user("anna")}
    for m in members:
        users[m] = await stack.provision_user(m)
    space_svc = SpaceService(
        _space_repo(stack.db),
        SqliteSpacePostRepo(stack.db),
        SqliteUserRepo(stack.db),
        stack.bus,
        own_instance_id="iid",
    )
    space = await space_svc.create_space(owner_username="anna", name=name)
    for m in members:
        await space_svc.add_member(
            space.id, actor_username="anna", user_id=users[m].user_id
        )
    for uname, level in (levels or {}).items():
        await stack.notif_repo.set_space_notif_level(
            user_id=users[uname].user_id, space_id=space.id, level=level
        )
    return space_svc, space, users


_CONTENT_TYPES = {"space_mention", "space_post_created", "space_comment_added"}


async def _types(stack, user):
    """Content-bell types for *user* (ignores roster bells like
    ``space_member_joined`` the setup itself produces), oldest first."""
    notes = await stack.notif_repo.list(user.user_id, limit=50)
    return [n.type for n in reversed(notes) if n.type in _CONTENT_TYPES]


async def test_space_post_mentions_only_level_end_to_end(stack):
    """level='mentions' — a real post via SpaceService: no bell for a plain
    post, a "mentioned you" bell once the post @-mentions the member."""
    space_svc, space, u = await _mention_space(stack, "bob", levels={"bob": "mentions"})
    await space_svc.create_post(
        space.id,
        author_user_id=u["anna"].user_id,
        type=PostType.TEXT,
        content="hi all",
    )
    assert await _types(stack, u["bob"]) == []
    await space_svc.create_post(
        space.id,
        author_user_id=u["anna"].user_id,
        type=PostType.TEXT,
        content="lunch, @bob?",
    )
    notes = [
        n
        for n in await stack.notif_repo.list(u["bob"].user_id, limit=50)
        if n.type in _CONTENT_TYPES
    ]
    assert [n.type for n in notes] == ["space_mention"]
    assert notes[0].title == "anna mentioned you in M"
    assert notes[0].link_url == f"/spaces/{space.id}"


async def test_space_post_mention_replaces_generic_for_level_all(stack):
    """A mentioned member at level 'all' gets one distinct mention bell,
    not a generic one on top; unmentioned members get the generic one."""
    space_svc, space, u = await _mention_space(stack, "bob", "carl")
    await space_svc.create_post(
        space.id,
        author_user_id=u["anna"].user_id,
        type=PostType.TEXT,
        content="@bob see this",
    )
    assert await _types(stack, u["bob"]) == ["space_mention"]
    assert await _types(stack, u["carl"]) == ["space_post_created"]


async def test_space_post_mention_muted_self_and_non_member(stack):
    """Muted gets nothing even when mentioned; the author never notifies
    themself; a household user outside the space is never reached."""
    space_svc, space, u = await _mention_space(stack, "bob", levels={"bob": "muted"})
    dave = await stack.provision_user("dave")  # not a member
    await space_svc.create_post(
        space.id,
        author_user_id=u["anna"].user_id,
        type=PostType.TEXT,
        content="@bob @anna @dave",
    )
    assert await _types(stack, u["bob"]) == []
    assert await _types(stack, u["anna"]) == []
    assert await _types(stack, dave) == []


async def test_space_post_at_here_does_not_notify_mentions_level(stack):
    """@here does nothing while the space's ``allow_here_mention`` is off
    (the default), even from the owner."""
    space_svc, space, u = await _mention_space(stack, "bob", levels={"bob": "mentions"})
    await space_svc.create_post(
        space.id,
        author_user_id=u["anna"].user_id,
        type=PostType.TEXT,
        content="@here standup",
    )
    assert await _types(stack, u["bob"]) == []


async def test_space_mention_push_is_title_only(stack):
    """§25.3 — the push for a mention carries the title, never the body."""
    space_svc, space, u = await _mention_space(stack, "bob")
    sent = []

    class _Push:
        async def push_to_user(self, user_id, payload):
            sent.append((user_id, payload))

    stack.notif_svc.attach_push_service(_Push())
    await space_svc.create_post(
        space.id,
        author_user_id=u["anna"].user_id,
        type=PostType.TEXT,
        content="@bob secret plans",
    )
    assert len(sent) == 1
    user_id, payload = sent[0]
    assert user_id == u["bob"].user_id
    assert payload.title == "anna mentioned you in M"
    assert "secret" not in payload.to_json()


async def test_moderated_post_mentions_notify_on_approval(stack):
    """A queued post's mentions fire when an admin approves it (same
    ``_persist_post`` path), attributed to the submitter."""

    space_svc, space, u = await _mention_space(stack, "bob", "carl")
    await space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(posts_access=SpaceFeatureAccess.MODERATED),
    )
    assert (
        await space_svc.create_post(
            space.id,
            author_user_id=u["bob"].user_id,
            type=PostType.TEXT,
            content="@carl hello",
        )
        is None
    )
    item = (await space_svc.list_pending_moderation(space.id, actor_username="anna"))[0]
    await space_svc.approve_moderation_item(space.id, item.id, actor_username="anna")
    notes = await stack.notif_repo.list(u["carl"].user_id, limit=50)
    assert [n.title for n in notes if n.type == "space_mention"] == [
        "bob mentioned you in M"
    ]


async def test_space_comment_mentions_and_member_scope(stack):
    """Space comments notify space members only (not the whole household),
    honour the level, and a mention gets its own bell."""
    space_svc, space, u = await _mention_space(
        stack, "bob", "carl", "erin", levels={"carl": "mentions", "erin": "muted"}
    )
    dave = await stack.provision_user("dave")  # household, not in the space
    post = await space_svc.create_post(
        space.id,
        author_user_id=u["anna"].user_id,
        type=PostType.TEXT,
        content="plain",
    )
    before = {k: await _types(stack, v) for k, v in u.items()}
    await space_svc.add_comment(
        post.id, author_user_id=u["bob"].user_id, content="agree @carl @erin"
    )
    # anna (level all, not mentioned) → generic comment bell.
    assert (await _types(stack, u["anna"]))[len(before["anna"]) :] == [
        "space_comment_added"
    ]
    # carl (level mentions, mentioned) → mention bell only.
    carl = [
        n
        for n in await stack.notif_repo.list(u["carl"].user_id, limit=50)
        if n.type in _CONTENT_TYPES
    ]
    assert [n.type for n in carl] == ["space_mention"]
    assert carl[0].title == "bob mentioned you in a comment in M"
    # erin muted, bob is the commenter, dave outside the space.
    assert await _types(stack, u["erin"]) == []
    assert (await _types(stack, u["bob"])) == before["bob"]
    assert await _types(stack, dave) == []
    # A plain comment skips the mentions-level member.
    await space_svc.add_comment(post.id, author_user_id=u["bob"].user_id, content="k")
    assert await _types(stack, u["carl"]) == ["space_mention"]


async def test_space_comment_in_missing_space_is_silent(stack):
    """A CommentAdded for a space this household no longer has → no-op."""

    b = await stack.provision_user("bob")
    await stack.bus.publish(
        CommentAdded(
            post_id="p1",
            comment=Comment(
                id="c1",
                post_id="p1",
                author="x",
                type=CommentType.TEXT,
                created_at=datetime.now(timezone.utc),
                content="hi",
            ),
            space_id="gone",
        )
    )
    assert await _types(stack, b) == []


async def test_moderation_queued_notifies_admins(stack):
    """SpaceModerationQueued notifies space admins."""
    from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
    from socialhome.services.space_service import SpaceService
    from socialhome.domain.space import SpaceFeatures, SpaceFeatureAccess

    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space_repo = _space_repo(stack.db)
    spost_repo = SqliteSpacePostRepo(stack.db)
    space_svc = SpaceService(
        space_repo,
        spost_repo,
        SqliteUserRepo(stack.db),
        stack.bus,
        own_instance_id="iid",
    )
    space = await space_svc.create_space(owner_username="anna", name="Mod")
    await space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    await space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(posts_access=SpaceFeatureAccess.MODERATED),
    )
    # Bob is regular member — post goes to queue → admin (anna) gets notification
    result = await space_svc.create_post(
        space.id, author_user_id=b.user_id, type=PostType.TEXT, content="pending"
    )
    assert result is None  # queued
    anna_n = await stack.notif_repo.list(a.user_id, limit=50)
    assert any("pending review" in n.title for n in anna_n)


async def test_moderation_queued_notifies_moderators_but_not_members(stack):
    """v_41 — moderators work the queue, so they are told about it; a plain
    member is not."""
    from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
    from socialhome.services.space_service import SpaceService
    from socialhome.domain.space import SpaceFeatures, SpaceFeatureAccess

    await stack.provision_user("anna")
    mo = await stack.provision_user("mo")
    b = await stack.provision_user("bob")
    c = await stack.provision_user("cara")
    space_svc = SpaceService(
        _space_repo(stack.db),
        SqliteSpacePostRepo(stack.db),
        SqliteUserRepo(stack.db),
        stack.bus,
        own_instance_id="iid",
    )
    space = await space_svc.create_space(owner_username="anna", name="Mod")
    for u in (mo, b, c):
        await space_svc.add_member(space.id, actor_username="anna", user_id=u.user_id)
    await space_svc.set_role(
        space.id, actor_username="anna", user_id=mo.user_id, role="moderator"
    )
    await space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(posts_access=SpaceFeatureAccess.MODERATED),
    )
    await space_svc.create_post(
        space.id, author_user_id=b.user_id, type=PostType.TEXT, content="pending"
    )
    mo_n = await stack.notif_repo.list(mo.user_id, limit=50)
    cara_n = await stack.notif_repo.list(c.user_id, limit=50)
    assert any("pending review" in n.title for n in mo_n)
    assert not any("pending review" in n.title for n in cara_n)


async def test_task_deadline_notifies_assignees(stack):
    """TaskDeadlineDue notifies all assignees."""
    from datetime import date
    from socialhome.domain.events import TaskDeadlineDue

    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    now = datetime.now(timezone.utc)
    evt = TaskDeadlineDue(
        task=Task(
            id="t1",
            list_id="l1",
            title="Deadline task",
            status=TaskStatus.TODO,
            position=0,
            created_by="other",
            created_at=now,
            updated_at=now,
            assignees=(a.user_id, b.user_id),
        ),
        due_date=date.today(),
    )
    await stack.bus.publish(evt)
    anna_n = await stack.notif_repo.list(a.user_id, limit=50)
    bob_n = await stack.notif_repo.list(b.user_id, limit=50)
    assert any("due today" in n.title for n in anna_n)
    assert any("due today" in n.title for n in bob_n)


# ─── Push fan-out (§25.3) ─────────────────────────────────────────────────


class _CapturingPush:
    """Fake PushService for assert-pushed tests.

    Captures both fan-out shapes:
    * ``push_to_users(ids, payload)`` — used by ``_fan_push``.
    * ``push_to_user(id, payload)``  — used by ``_save_notif`` for
      the per-row Web Push fan-out.

    The combined ``calls`` log preserves the order so a single test
    can assert across both code paths.
    """

    def __init__(self):
        self.calls: list[tuple[list[str], object]] = []

    async def push_to_users(self, user_ids, payload):
        self.calls.append((list(user_ids), payload))
        return len(user_ids)

    async def push_to_user(self, user_id, payload):
        self.calls.append(([user_id], payload))
        return 1


async def _group(stack, conv_id: str, *users) -> None:
    await stack.conv_repo.create(
        Conversation(
            id=conv_id,
            type=ConversationType.GROUP_DM,
            created_at=datetime.now(timezone.utc),
        )
    )
    for u in users:
        await stack.conv_repo.add_member(
            ConversationMember(
                conversation_id=conv_id,
                username=u.username,
                joined_at=datetime.now(timezone.utc).isoformat(),
            )
        )


async def test_dm_to_a_muted_recipient_makes_no_row_and_no_push(stack):
    """Bob muted the conversation: no bell row and no push for him, while
    Carol (not muted) is notified as usual."""
    anna = await stack.provision_user("anna-m")
    bob = await stack.provision_user("bob-m")
    carol = await stack.provision_user("carol-m")
    await _group(stack, "c-muted", anna, bob, carol)
    await stack.conv_repo.set_muted_until("c-muted", bob.username, MUTED_FOREVER)
    push = _CapturingPush()
    stack.notif_svc.attach_push_service(push)

    await stack.bus.publish(
        DmMessageCreated(
            conversation_id="c-muted",
            message_id="m-1",
            sender_user_id=anna.user_id,
            sender_display_name="Anna",
            recipient_user_ids=(bob.user_id, carol.user_id),
        )
    )
    assert await stack.notif_repo.list(bob.user_id) == []
    assert len(await stack.notif_repo.list(carol.user_id)) == 1
    pushed_to = [uid for ids, _ in push.calls for uid in ids]
    assert pushed_to == [carol.user_id]


async def test_dm_after_the_mute_ran_out_notifies_again(stack):
    """A ``muted_until`` in the past reads as unmuted — no scheduler."""
    anna = await stack.provision_user("anna-x")
    bob = await stack.provision_user("bob-x")
    await _group(stack, "c-expired", anna, bob)
    await stack.conv_repo.set_muted_until(
        "c-expired", bob.username, "2020-01-01T00:00:00+00:00"
    )
    push = _CapturingPush()
    stack.notif_svc.attach_push_service(push)

    await stack.bus.publish(
        DmMessageCreated(
            conversation_id="c-expired",
            message_id="m-1",
            sender_user_id=anna.user_id,
            sender_display_name="Anna",
            recipient_user_ids=(bob.user_id,),
        )
    )
    assert len(await stack.notif_repo.list(bob.user_id)) == 1
    assert push.calls


async def test_dm_without_a_conversation_repo_never_reads_as_muted(stack):
    """No repo wired (older stacks): every recipient is notified."""
    anna = await stack.provision_user("anna-n")
    bob = await stack.provision_user("bob-n")
    await _group(stack, "c-norepo", anna, bob)
    await stack.conv_repo.set_muted_until("c-norepo", bob.username, MUTED_FOREVER)
    svc = NotificationService(
        stack.notif_repo, stack.notif_svc._users, stack.space_repo, EventBus()
    )
    await svc.on_dm_message_created(
        DmMessageCreated(
            conversation_id="c-norepo",
            message_id="m-1",
            sender_user_id=anna.user_id,
            sender_display_name="Anna",
            recipient_user_ids=(bob.user_id,),
        )
    )
    assert len(await stack.notif_repo.list(bob.user_id)) == 1


def _m(user) -> Mention:
    return Mention(type=MentionType.USER, raw=f"@{user.username}", user_id=user.user_id)


async def _named_group(stack, conv_id: str, name: str | None, *users) -> None:
    await stack.conv_repo.create(
        Conversation(
            id=conv_id,
            type=ConversationType.GROUP_DM,
            created_at=datetime.now(timezone.utc),
            name=name,
        )
    )
    for u in users:
        await stack.conv_repo.add_member(
            ConversationMember(
                conversation_id=conv_id,
                username=u.username,
                joined_at=datetime.now(timezone.utc).isoformat(),
            )
        )


def _dm_event(conv_id, sender, recipients, *, mentions=(), mid="m-1"):
    return DmMessageCreated(
        conversation_id=conv_id,
        message_id=mid,
        sender_user_id=sender.user_id,
        sender_display_name=sender.display_name,
        recipient_user_ids=tuple(r.user_id for r in recipients),
        content="x",
        mentions=tuple(mentions),
    )


async def test_group_mentions_level_only_rings_for_a_mention(stack):
    """level='mentions': a plain message rings nothing; a message that
    @-mentions the member gives one distinct ``dm_mention`` bell."""
    anna = await stack.provision_user("anna-l")
    bob = await stack.provision_user("bob-l")
    await _named_group(stack, "g-lvl", "Team", anna, bob)
    await stack.conv_repo.set_notif_level("g-lvl", bob.username, "mentions")
    await stack.bus.publish(_dm_event("g-lvl", anna, [bob]))
    assert await stack.notif_repo.list(bob.user_id) == []
    await stack.bus.publish(
        _dm_event("g-lvl", anna, [bob], mentions=[_m(bob)], mid="m-2")
    )
    notes = await stack.notif_repo.list(bob.user_id)
    assert [(n.type, n.title, n.link_url) for n in notes] == [
        ("dm_mention", "anna-l mentioned you in Team", "/dms/g-lvl")
    ]


async def test_group_mention_at_level_all_replaces_the_message_bell(stack):
    """Level 'all' + mentioned → the mention bell only, never both; an
    unmentioned member gets the ordinary message bell."""
    anna = await stack.provision_user("anna-a")
    bob = await stack.provision_user("bob-a")
    carl = await stack.provision_user("carl-a")
    await _named_group(stack, "g-all", None, anna, bob, carl)
    push = _CapturingPush()
    stack.notif_svc.attach_push_service(push)
    await stack.bus.publish(_dm_event("g-all", anna, [bob, carl], mentions=[_m(bob)]))
    assert [n.type for n in await stack.notif_repo.list(bob.user_id)] == ["dm_mention"]
    assert (await stack.notif_repo.list(bob.user_id))[0].title == (
        "anna-a mentioned you in a group chat"
    )
    assert [n.type for n in await stack.notif_repo.list(carl.user_id)] == ["dm_message"]
    # §25.3: the push carries the title only.
    for _ids, payload in push.calls:
        assert not getattr(payload, "body", None)
        assert "x" != payload.title


async def test_group_mention_of_a_muted_member_or_self_is_silent(stack):
    anna = await stack.provision_user("anna-q")
    bob = await stack.provision_user("bob-q")
    await _named_group(stack, "g-mute", "G", anna, bob)
    await stack.conv_repo.set_muted_until("g-mute", bob.username, MUTED_FOREVER)
    await stack.bus.publish(
        _dm_event("g-mute", anna, [bob], mentions=[_m(bob), _m(anna)])
    )
    assert await stack.notif_repo.list(bob.user_id) == []
    assert await stack.notif_repo.list(anna.user_id) == []


async def test_one_to_one_mention_is_an_ordinary_message_bell(stack):
    """In a 1:1 every message is for the other person — no mention bell."""
    anna = await stack.provision_user("anna-o")
    bob = await stack.provision_user("bob-o")
    await stack.conv_repo.create(
        Conversation(
            id="d-1", type=ConversationType.DM, created_at=datetime.now(timezone.utc)
        )
    )
    for u in (anna, bob):
        await stack.conv_repo.add_member(
            ConversationMember(
                conversation_id="d-1",
                username=u.username,
                joined_at=datetime.now(timezone.utc).isoformat(),
            )
        )
    await stack.bus.publish(_dm_event("d-1", anna, [bob], mentions=[_m(bob)]))
    assert [n.type for n in await stack.notif_repo.list(bob.user_id)] == ["dm_message"]


async def test_dm_edit_notifies_only_the_newly_mentioned(stack):
    """An edit rings only the members it newly mentions (dm_mention), even at
    level 'mentions'; muted members and the sender stay silent."""
    anna = await stack.provision_user("anna-e")
    bob = await stack.provision_user("bob-e")
    carl = await stack.provision_user("carl-e")
    dora = await stack.provision_user("dora-e")
    await _named_group(stack, "g-edit", "Team", anna, bob, carl, dora)
    await stack.conv_repo.set_notif_level("g-edit", carl.username, "mentions")
    await stack.conv_repo.set_muted_until("g-edit", dora.username, MUTED_FOREVER)
    await stack.bus.publish(
        DmMessageUpdated(
            conversation_id="g-edit",
            message_id="m-1",
            sender_user_id=anna.user_id,
            recipient_user_ids=(bob.user_id, carl.user_id, dora.user_id),
            content="now @carl-e and @dora-e",
            edited_at=datetime.now(timezone.utc),
            new_mentions=(_m(carl), _m(dora), _m(anna)),
            sender_display_name="Anna",
        )
    )
    assert await stack.notif_repo.list(bob.user_id) == []
    notes = await stack.notif_repo.list(carl.user_id)
    assert [(n.type, n.title) for n in notes] == [
        ("dm_mention", "Anna mentioned you in Team")
    ]
    assert await stack.notif_repo.list(dora.user_id) == []
    assert await stack.notif_repo.list(anna.user_id) == []


async def test_dm_edit_without_new_mentions_is_silent(stack):
    anna = await stack.provision_user("anna-s")
    bob = await stack.provision_user("bob-s")
    await _named_group(stack, "g-s", "S", anna, bob)
    await stack.bus.publish(
        DmMessageUpdated(
            conversation_id="g-s",
            message_id="m-1",
            sender_user_id=anna.user_id,
            recipient_user_ids=(bob.user_id,),
            content="typo fixed",
            edited_at=datetime.now(timezone.utc),
        )
    )
    assert await stack.notif_repo.list(bob.user_id) == []


async def test_mark_read_for_dm_also_clears_mention_rows(stack):
    anna = await stack.provision_user("anna-r")
    bob = await stack.provision_user("bob-r")
    await _named_group(stack, "g-r", "R", anna, bob)
    await stack.bus.publish(_dm_event("g-r", anna, [bob], mentions=[_m(bob)]))
    assert await stack.notif_repo.count_unread(bob.user_id) == 1
    await stack.notif_svc.mark_read_for_dm(bob.user_id, "g-r")
    assert await stack.notif_repo.count_unread(bob.user_id) == 0


async def test_dm_message_creates_in_app_row_and_push(stack):
    """A new DM creates an in-app notification row per recipient (so
    the bell renders an unread badge) AND fires push (§25.3 — title
    only, no body)."""
    from socialhome.domain.events import DmMessageCreated

    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    fake = _CapturingPush()
    stack.notif_svc.attach_push_service(fake)

    await stack.bus.publish(
        DmMessageCreated(
            conversation_id="c-1",
            message_id="m-1",
            sender_user_id=a.user_id,
            sender_display_name="Anna",
            recipient_user_ids=(b.user_id,),
        )
    )

    # In-app row landed for the recipient.
    rows = await stack.notif_repo.list(b.user_id)
    assert len(rows) == 1
    assert rows[0].type == "dm_message"
    assert rows[0].link_url == "/dms/c-1"
    assert "Anna" in rows[0].title

    # Push went out too — title only, no body.
    assert fake.calls, "push fan-out was not triggered"
    _, payload = fake.calls[0]
    assert "Anna" in payload.title
    # §25.3: the PushPayload struct has no body field at all.
    assert not hasattr(payload, "body")


async def test_dm_location_message_title_only_no_coordinates(stack):
    """A shared location notifies as "X shared a location" — title only,
    and no coordinate or label ever reaches the bell row or the push."""
    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    fake = _CapturingPush()
    stack.notif_svc.attach_push_service(fake)

    await stack.bus.publish(
        DmMessageCreated(
            conversation_id="c-loc",
            message_id="m-loc",
            sender_user_id=a.user_id,
            sender_display_name="Anna",
            recipient_user_ids=(b.user_id,),
            content='{"lat":52.3702,"lon":4.8952,"label":"Secret spot","accuracy_m":25}',
            message_type="location",
        )
    )

    rows = await stack.notif_repo.list(b.user_id)
    assert len(rows) == 1
    assert rows[0].title == "Anna shared a location"
    assert fake.calls
    _, payload = fake.calls[0]
    assert payload.title == "Anna shared a location"
    assert not hasattr(payload, "body")
    flat = repr(rows[0]) + repr(payload)
    for leak in ("52.3702", "4.8952", "Secret spot"):
        assert leak not in flat


async def test_dm_message_creates_one_row_per_recipient(stack):
    """Group DMs fan one notification row to each recipient (and push
    too) so every member gets their own bell badge — bell counts
    don't get coalesced server-side."""
    from socialhome.domain.events import DmMessageCreated

    sender = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    carol = await stack.provision_user("carol")
    stack.notif_svc.attach_push_service(_CapturingPush())

    await stack.bus.publish(
        DmMessageCreated(
            conversation_id="c-group",
            message_id="m-1",
            sender_user_id=sender.user_id,
            sender_display_name="Anna",
            recipient_user_ids=(bob.user_id, carol.user_id),
        )
    )
    bob_rows = await stack.notif_repo.list(bob.user_id)
    carol_rows = await stack.notif_repo.list(carol.user_id)
    assert len(bob_rows) == 1
    assert len(carol_rows) == 1
    assert bob_rows[0].link_url == "/dms/c-group"


async def test_dm_burst_collapses_to_single_unread_row(stack):
    """A burst of DMs from the same sender to the same recipient
    bumps one bell row instead of stacking N entries."""
    from socialhome.domain.events import DmMessageCreated

    sender = await stack.provision_user("anna-burst")
    bob = await stack.provision_user("bob-burst")
    stack.notif_svc.attach_push_service(_CapturingPush())

    for mid in ("m-1", "m-2", "m-3", "m-4", "m-5"):
        await stack.bus.publish(
            DmMessageCreated(
                conversation_id="c-burst",
                message_id=mid,
                sender_user_id=sender.user_id,
                sender_display_name="Anna",
                recipient_user_ids=(bob.user_id,),
            )
        )

    rows = await stack.notif_repo.list(bob.user_id, limit=50)
    dm_rows = [
        r for r in rows if r.type == "dm_message" and r.link_url == "/dms/c-burst"
    ]
    assert len(dm_rows) == 1
    assert dm_rows[0].read_at is None


async def test_dm_dedupe_does_not_span_read_boundary(stack):
    """Once the recipient opens the thread, the next DM starts a
    fresh unread row rather than re-using the now-read one."""
    from socialhome.domain.events import DmMessageCreated

    sender = await stack.provision_user("anna-rb")
    bob = await stack.provision_user("bob-rb")
    stack.notif_svc.attach_push_service(_CapturingPush())

    await stack.bus.publish(
        DmMessageCreated(
            conversation_id="c-rb",
            message_id="m-1",
            sender_user_id=sender.user_id,
            sender_display_name="Anna",
            recipient_user_ids=(bob.user_id,),
        )
    )
    assert await stack.notif_repo.count_unread(bob.user_id) == 1
    # Open the thread — clears the row.
    await stack.notif_svc.mark_read_for_dm(bob.user_id, "c-rb")
    assert await stack.notif_repo.count_unread(bob.user_id) == 0
    # New DM after read → new unread row, not a bump of the read one.
    await stack.bus.publish(
        DmMessageCreated(
            conversation_id="c-rb",
            message_id="m-2",
            sender_user_id=sender.user_id,
            sender_display_name="Anna",
            recipient_user_ids=(bob.user_id,),
        )
    )
    assert await stack.notif_repo.count_unread(bob.user_id) == 1
    rows = await stack.notif_repo.list(bob.user_id, limit=10)
    dm_rows = [r for r in rows if r.type == "dm_message"]
    # Two rows total: one read (from the first burst) + one new unread.
    assert len(dm_rows) == 2
    assert sum(1 for r in dm_rows if r.read_at is None) == 1


async def test_dm_message_skipped_when_recipient_viewing_thread(stack):
    """When the recipient has the DM thread open in any of their tabs
    (SPA emits ``dm.active`` over WS), the notification service skips
    both the bell row AND the push fan-out. The message itself still
    renders via the regular DM broadcast path; only the notification
    noise is suppressed.
    """
    from socialhome.domain.events import DmMessageCreated

    class _FakeWsMgr:
        def __init__(self, active_conv: dict[str, str | None]) -> None:
            self._active = active_conv

        def is_user_active_in_conversation(
            self, user_id: str, conversation_id: str
        ) -> bool:
            return self._active.get(user_id) == conversation_id

    sender = await stack.provision_user("anna-av")
    bob = await stack.provision_user("bob-av")
    carol = await stack.provision_user("carol-av")

    push = _CapturingPush()
    stack.notif_svc.attach_push_service(push)
    # Bob is on the thread; Carol is not.
    stack.notif_svc.attach_ws_manager(
        _FakeWsMgr({bob.user_id: "c-av", carol.user_id: None})
    )

    await stack.bus.publish(
        DmMessageCreated(
            conversation_id="c-av",
            message_id="m-av-1",
            sender_user_id=sender.user_id,
            sender_display_name="Anna",
            recipient_user_ids=(bob.user_id, carol.user_id),
        )
    )

    pushed_to = {uid for user_ids, _ in push.calls for uid in user_ids}

    # Bob (viewing) — no bell row, no push.
    assert await stack.notif_repo.count_unread(bob.user_id) == 0
    assert bob.user_id not in pushed_to, (
        "push fired for bob even though he had the thread open"
    )

    # Carol (not viewing) — gets the notification as usual.
    assert await stack.notif_repo.count_unread(carol.user_id) == 1
    assert carol.user_id in pushed_to, "push did not fire for carol who wasn't viewing"


async def test_mark_read_for_dm_clears_unread_rows(stack):
    """``mark_read_for_dm`` flips the (collapsed) ``dm_message`` row
    for a conversation to read — opening the thread clears the bell
    in step with the read-receipt update.

    Note: rows are deduped per conversation, so a 2-message burst
    bumps a single bell row rather than producing two.
    """
    from socialhome.domain.events import DmMessageCreated

    sender = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    stack.notif_svc.attach_push_service(_CapturingPush())

    # Two messages from Anna to Bob → one (bumped) row.
    for mid in ("m-1", "m-2"):
        await stack.bus.publish(
            DmMessageCreated(
                conversation_id="c-1",
                message_id=mid,
                sender_user_id=sender.user_id,
                sender_display_name="Anna",
                recipient_user_ids=(bob.user_id,),
            )
        )
    assert await stack.notif_repo.count_unread(bob.user_id) == 1

    # Open the thread → the row clears.
    n = await stack.notif_svc.mark_read_for_dm(bob.user_id, "c-1")
    assert n == 1
    assert await stack.notif_repo.count_unread(bob.user_id) == 0


async def test_mark_read_for_dm_only_touches_matching_conversation(stack):
    """A different conversation's notifications stay unread when
    one specific thread is opened."""
    from socialhome.domain.events import DmMessageCreated

    sender = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    stack.notif_svc.attach_push_service(_CapturingPush())

    for cid in ("c-1", "c-2"):
        await stack.bus.publish(
            DmMessageCreated(
                conversation_id=cid,
                message_id=f"m-{cid}",
                sender_user_id=sender.user_id,
                sender_display_name="Anna",
                recipient_user_ids=(bob.user_id,),
            )
        )
    assert await stack.notif_repo.count_unread(bob.user_id) == 2

    cleared = await stack.notif_svc.mark_read_for_dm(bob.user_id, "c-1")
    assert cleared == 1
    # The c-2 row stays unread.
    assert await stack.notif_repo.count_unread(bob.user_id) == 1


async def test_dm_message_with_no_recipients_skips_push(stack):
    from socialhome.domain.events import DmMessageCreated

    a = await stack.provision_user("anna")
    fake = _CapturingPush()
    stack.notif_svc.attach_push_service(fake)

    await stack.bus.publish(
        DmMessageCreated(
            conversation_id="c-1",
            message_id="m-1",
            sender_user_id=a.user_id,
            sender_display_name="Anna",
            recipient_user_ids=(),
        )
    )
    assert fake.calls == []


async def test_task_deadline_triggers_push(stack):
    from datetime import date
    from socialhome.domain.events import TaskDeadlineDue

    a = await stack.provision_user("anna")
    fake = _CapturingPush()
    stack.notif_svc.attach_push_service(fake)
    now = datetime.now(timezone.utc)
    evt = TaskDeadlineDue(
        task=Task(
            id="t1",
            list_id="l1",
            title="Pay bills",
            status=TaskStatus.TODO,
            position=0,
            created_by="other",
            created_at=now,
            updated_at=now,
            assignees=(a.user_id,),
        ),
        due_date=date.today(),
    )
    await stack.bus.publish(evt)
    assert fake.calls
    _, payload = fake.calls[-1]
    assert "Pay bills" in payload.title


# ─── Bazaar + DM contact handlers ─────────────────────────────────────────


async def test_bazaar_bid_placed_notifies_seller(stack):
    from socialhome.domain.events import BazaarBidPlaced

    seller = await stack.provision_user("seller")
    bidder = await stack.provision_user("bidder")
    fake = _CapturingPush()
    stack.notif_svc.attach_push_service(fake)
    await stack.bus.publish(
        BazaarBidPlaced(
            listing_post_id="L-1",
            seller_user_id=seller.user_id,
            bidder_user_id=bidder.user_id,
            amount=200,
            new_end_time="2099-01-01T00:00:00+00:00",
        )
    )
    notifs = await stack.notif_repo.list(seller.user_id, limit=10)
    assert any(n.type == "bazaar_bid_placed" for n in notifs)
    assert fake.calls
    assert fake.calls[-1][0] == [seller.user_id]


async def test_bazaar_self_bid_does_not_notify(stack):
    from socialhome.domain.events import BazaarBidPlaced

    seller = await stack.provision_user("seller")
    fake = _CapturingPush()
    stack.notif_svc.attach_push_service(fake)
    await stack.bus.publish(
        BazaarBidPlaced(
            listing_post_id="L-1",
            seller_user_id=seller.user_id,
            bidder_user_id=seller.user_id,
            amount=200,
            new_end_time="2099-01-01T00:00:00+00:00",
        )
    )
    notifs = await stack.notif_repo.list(seller.user_id, limit=10)
    assert all(n.type != "bazaar_bid_placed" for n in notifs)
    assert fake.calls == []


async def test_bazaar_offer_accepted_notifies_buyer(stack):
    from socialhome.domain.events import BazaarOfferAccepted

    seller = await stack.provision_user("seller")
    buyer = await stack.provision_user("buyer")
    fake = _CapturingPush()
    stack.notif_svc.attach_push_service(fake)
    await stack.bus.publish(
        BazaarOfferAccepted(
            listing_post_id="L-1",
            seller_user_id=seller.user_id,
            buyer_user_id=buyer.user_id,
            price=200,
        )
    )
    notifs = await stack.notif_repo.list(buyer.user_id, limit=10)
    assert any(n.type == "bazaar_offer_accepted" for n in notifs)


async def test_dm_contact_request_notifies_recipient(stack):
    from socialhome.domain.events import DmContactRequested

    recipient = await stack.provision_user("recipient")
    fake = _CapturingPush()
    stack.notif_svc.attach_push_service(fake)
    await stack.bus.publish(
        DmContactRequested(
            requester_user_id="u-other",
            requester_display_name="Outside Friend",
            recipient_user_id=recipient.user_id,
        )
    )
    notifs = await stack.notif_repo.list(recipient.user_id, limit=10)
    assert any(n.type == "dm_contact_requested" for n in notifs)
    assert fake.calls
    title = fake.calls[-1][1].title
    assert "Outside Friend" in title


# ─── CalendarEventCreated handler ──────────────────────────────────────


async def test_calendar_event_created_on_personal_calendar_notifies_owner_only(stack):
    """When someone adds an event to a user's personal calendar, only
    that calendar's owner should get a bell — not every household
    member. The creator themselves is excluded."""
    from socialhome.domain.calendar import Calendar, CalendarEvent
    from socialhome.domain.events import CalendarEventCreated

    alice = await stack.provision_user("alice-cal")
    bob = await stack.provision_user("bob-cal")
    carol = await stack.provision_user("carol-cal")
    # Bob's personal calendar; Alice adds an event onto it (could be a
    # household "this is on your calendar" obligation).
    bobs_cal = Calendar(
        id="cal-bob",
        name="Bob",
        color="#4A90E2",
        owner_username=bob.username,
        calendar_type="personal",
    )
    await stack.calendar_repo.save_calendar(bobs_cal)
    event = CalendarEvent(
        id="e1",
        calendar_id="cal-bob",
        summary="Dentist appointment",
        created_by=alice.user_id,
        start=datetime(2026, 5, 1, 10, tzinfo=timezone.utc),
        end=datetime(2026, 5, 1, 11, tzinfo=timezone.utc),
    )
    await stack.bus.publish(CalendarEventCreated(event=event))
    # Owner gets the bell.
    assert any(
        n.type == "calendar_event_created"
        for n in await stack.notif_repo.list(bob.user_id, limit=10)
    )
    # Creator does not.
    assert not any(
        n.type == "calendar_event_created"
        for n in await stack.notif_repo.list(alice.user_id, limit=10)
    )
    # Unrelated household member does not.
    assert not any(
        n.type == "calendar_event_created"
        for n in await stack.notif_repo.list(carol.user_id, limit=10)
    )


async def test_calendar_event_created_on_own_calendar_notifies_nobody(stack):
    """Adding an event to your own personal calendar must not
    self-notify."""
    from socialhome.domain.calendar import Calendar, CalendarEvent
    from socialhome.domain.events import CalendarEventCreated

    alice = await stack.provision_user("alice-self")
    bob = await stack.provision_user("bob-self")
    cal = Calendar(
        id="cal-alice",
        name="Alice",
        color="#4A90E2",
        owner_username=alice.username,
        calendar_type="personal",
    )
    await stack.calendar_repo.save_calendar(cal)
    event = CalendarEvent(
        id="e-self",
        calendar_id="cal-alice",
        summary="Lift weights",
        created_by=alice.user_id,
        start=datetime(2026, 5, 1, 7, tzinfo=timezone.utc),
        end=datetime(2026, 5, 1, 8, tzinfo=timezone.utc),
    )
    await stack.bus.publish(CalendarEventCreated(event=event))
    assert not any(
        n.type == "calendar_event_created"
        for n in await stack.notif_repo.list(alice.user_id, limit=10)
    )
    assert not any(
        n.type == "calendar_event_created"
        for n in await stack.notif_repo.list(bob.user_id, limit=10)
    )


async def test_calendar_event_created_on_space_calendar_notifies_members(stack):
    """A space calendar event has no row in ``calendars`` — the
    ``calendar_id`` is the space_id directly. Recipients are the
    space's members (except the creator)."""
    from socialhome.domain.calendar import CalendarEvent
    from socialhome.domain.events import CalendarEventCreated
    from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
    from socialhome.services.space_service import SpaceService

    alice = await stack.provision_user("alice-sp")
    bob = await stack.provision_user("bob-sp")
    carol = await stack.provision_user("carol-sp")  # not a space member
    spost_repo = SqliteSpacePostRepo(stack.db)
    space_svc = SpaceService(
        stack.space_repo,
        spost_repo,
        SqliteUserRepo(stack.db),
        stack.bus,
        own_instance_id="iid",
    )
    space = await space_svc.create_space(owner_username="alice-sp", name="Crew")
    await space_svc.add_member(space.id, actor_username="alice-sp", user_id=bob.user_id)
    event = CalendarEvent(
        id="se1",
        calendar_id=space.id,
        summary="Saturday ride",
        created_by=alice.user_id,
        start=datetime(2026, 5, 1, 9, tzinfo=timezone.utc),
        end=datetime(2026, 5, 1, 12, tzinfo=timezone.utc),
    )
    await stack.bus.publish(CalendarEventCreated(event=event))
    # Member gets it.
    assert any(
        n.type == "calendar_event_created"
        for n in await stack.notif_repo.list(bob.user_id, limit=10)
    )
    # Creator (also a member) does not.
    assert not any(
        n.type == "calendar_event_created"
        for n in await stack.notif_repo.list(alice.user_id, limit=10)
    )
    # Non-member doesn't either.
    assert not any(
        n.type == "calendar_event_created"
        for n in await stack.notif_repo.list(carol.user_id, limit=10)
    )


async def test_remote_space_dissolved_notifies_each_member(stack):
    """A remote SPACE_DISSOLVED archived the space read-only; every local
    member gets a one-time ``space_dissolved`` notification (the space is
    still viewable as an archive)."""
    from socialhome.domain.events import RemoteSpaceDissolved
    from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
    from socialhome.services.space_service import SpaceService

    alice = await stack.provision_user("alice-rd")
    bob = await stack.provision_user("bob-rd")
    spost_repo = SqliteSpacePostRepo(stack.db)
    space_svc = SpaceService(
        stack.space_repo,
        spost_repo,
        SqliteUserRepo(stack.db),
        stack.bus,
        own_instance_id="iid",
    )
    space = await space_svc.create_space(owner_username="alice-rd", name="Crew")
    await space_svc.add_member(space.id, actor_username="alice-rd", user_id=bob.user_id)

    await stack.bus.publish(RemoteSpaceDissolved(space_id=space.id))

    for user in (alice, bob):
        notes = await stack.notif_repo.list(user.user_id, limit=10)
        dissolved = [n for n in notes if n.type == "space_dissolved"]
        assert len(dissolved) == 1, user.user_id
        assert space.name in dissolved[0].title
        assert dissolved[0].link_url == f"/spaces/{space.id}"


async def test_remote_space_dissolved_rebroadcast_dedupes(stack):
    """A re-broadcast of SPACE_DISSOLVED (fresh msg_id) must not re-notify
    every member. Publishing ``RemoteSpaceDissolved`` twice for the same
    space yields ONE unread ``space_dissolved`` row per member, not two."""
    from socialhome.domain.events import RemoteSpaceDissolved
    from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
    from socialhome.services.space_service import SpaceService

    alice = await stack.provision_user("alice-rdd")
    bob = await stack.provision_user("bob-rdd")
    spost_repo = SqliteSpacePostRepo(stack.db)
    space_svc = SpaceService(
        stack.space_repo,
        spost_repo,
        SqliteUserRepo(stack.db),
        stack.bus,
        own_instance_id="iid",
    )
    space = await space_svc.create_space(owner_username="alice-rdd", name="Crew")
    await space_svc.add_member(
        space.id, actor_username="alice-rdd", user_id=bob.user_id
    )

    await stack.bus.publish(RemoteSpaceDissolved(space_id=space.id))
    await stack.bus.publish(RemoteSpaceDissolved(space_id=space.id))  # re-broadcast

    for user in (alice, bob):
        notes = await stack.notif_repo.list(user.user_id, limit=10)
        dissolved = [
            n for n in notes if n.type == "space_dissolved" and n.read_at is None
        ]
        assert len(dissolved) == 1, user.user_id


async def test_remote_space_dissolved_unknown_space_is_noop(stack):
    from socialhome.domain.events import RemoteSpaceDissolved

    await stack.bus.publish(RemoteSpaceDissolved(space_id="nope"))  # no raise


async def test_remote_space_dissolved_reason_dissolved_keeps_was_dissolved(stack):
    """When the space's archived_reason is 'dissolved', the notice keeps the
    'was dissolved' wording."""
    from socialhome.domain.events import RemoteSpaceDissolved
    from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
    from socialhome.services.space_service import SpaceService

    alice = await stack.provision_user("alice-rdis")
    spost_repo = SqliteSpacePostRepo(stack.db)
    space_svc = SpaceService(
        stack.space_repo,
        spost_repo,
        SqliteUserRepo(stack.db),
        stack.bus,
        own_instance_id="iid",
    )
    space = await space_svc.create_space(owner_username="alice-rdis", name="Crew")
    await stack.space_repo.set_archived(space.id, True, reason="dissolved")

    await stack.bus.publish(RemoteSpaceDissolved(space_id=space.id))

    notes = await stack.notif_repo.list(alice.user_id, limit=10)
    dissolved = [n for n in notes if n.type == "space_dissolved"]
    assert len(dissolved) == 1
    assert "was dissolved" in dissolved[0].title
    assert space.name in dissolved[0].title


async def test_remote_space_dissolved_reason_removed_says_no_longer_member(stack):
    """When the space's archived_reason is 'removed' (we were removed from a
    still-existing space), the notice reads 'no longer a member'."""
    from socialhome.domain.events import RemoteSpaceDissolved
    from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
    from socialhome.services.space_service import SpaceService

    alice = await stack.provision_user("alice-rrem")
    spost_repo = SqliteSpacePostRepo(stack.db)
    space_svc = SpaceService(
        stack.space_repo,
        spost_repo,
        SqliteUserRepo(stack.db),
        stack.bus,
        own_instance_id="iid",
    )
    space = await space_svc.create_space(owner_username="alice-rrem", name="Crew")
    await stack.space_repo.set_archived(space.id, True, reason="removed")

    await stack.bus.publish(RemoteSpaceDissolved(space_id=space.id))

    notes = await stack.notif_repo.list(alice.user_id, limit=10)
    removed = [n for n in notes if n.type == "space_dissolved"]
    assert len(removed) == 1
    assert "no longer a member" in removed[0].title
    assert space.name in removed[0].title
    assert removed[0].link_url == f"/spaces/{space.id}"


# ─── TaskCompleted handler ─────────────────────────────────────────────


async def test_task_completed_notifies_assignees(stack):
    from socialhome.domain.events import TaskCompleted

    alice = await stack.provision_user("alice-tc")
    bob = await stack.provision_user("bob-tc")
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    task = Task(
        id="t1",
        list_id="l1",
        title="Buy milk",
        status=TaskStatus.DONE,
        position=0,
        created_by="me",
        created_at=now,
        updated_at=now,
        assignees=(bob.user_id,),
    )
    await stack.bus.publish(
        TaskCompleted(
            task=task,
            completed_by=alice.user_id,
        )
    )
    notifs = await stack.notif_repo.list(bob.user_id, limit=10)
    assert any(n.type == "task_completed" for n in notifs)


async def _space_task_env(stack, sid: str, member_ids: list[str]) -> None:
    owner = await stack.provision_user(f"owner-{sid}")
    await stack.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?, 'S', 'inst', ?, ?)",
        (sid, owner.username, "ab" * 32),
    )
    for uid in member_ids:
        await stack.db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'member')",
            (sid, uid),
        )


def _space_task(*, assignees=(), status=TaskStatus.TODO) -> Task:
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    return Task(
        id="st1",
        list_id="sl1",
        title="Space secret",
        status=status,
        position=0,
        created_by="me",
        created_at=now,
        updated_at=now,
        assignees=assignees,
    )


async def test_space_task_assigned_skips_a_non_member(stack):
    from socialhome.domain.events import TaskAssigned

    member = await stack.provision_user("member-ta")
    outsider = await stack.provision_user("outsider-ta")
    await _space_task_env(stack, "sp-ta", [member.user_id])
    for uid in (member.user_id, outsider.user_id):
        await stack.bus.publish(
            TaskAssigned(task=_space_task(), assigned_to=uid, space_id="sp-ta")
        )
    assert await stack.notif_repo.list(outsider.user_id, limit=10) == []
    got = await stack.notif_repo.list(member.user_id, limit=10)
    assert [n.type for n in got] == ["task_assigned"]


async def test_space_task_completed_links_to_the_space_tasks_tab(stack):
    from socialhome.domain.events import TaskCompleted

    alice = await stack.provision_user("alice-stc")
    bob = await stack.provision_user("bob-stc")
    outsider = await stack.provision_user("outsider-stc")
    await _space_task_env(stack, "sp-tc", [alice.user_id, bob.user_id])
    await stack.bus.publish(
        TaskCompleted(
            task=_space_task(
                assignees=(bob.user_id, outsider.user_id), status=TaskStatus.DONE
            ),
            completed_by=alice.user_id,
            space_id="sp-tc",
        )
    )
    notifs = await stack.notif_repo.list(bob.user_id, limit=10)
    assert [n.link_url for n in notifs] == ["/spaces/sp-tc?tab=tasks"]
    assert await stack.notif_repo.list(outsider.user_id, limit=10) == []


async def test_household_task_completed_keeps_the_tasks_link(stack):
    from socialhome.domain.events import TaskCompleted

    alice = await stack.provision_user("alice-htc")
    bob = await stack.provision_user("bob-htc")
    await stack.bus.publish(
        TaskCompleted(
            task=_space_task(assignees=(bob.user_id,), status=TaskStatus.DONE),
            completed_by=alice.user_id,
        )
    )
    notifs = await stack.notif_repo.list(bob.user_id, limit=10)
    assert [n.link_url for n in notifs] == ["/tasks/sl1"]


# ─── SpacePostModerated handler ───────────────────────────────────────


async def test_space_post_moderated_notifies_author(stack):
    from socialhome.domain.events import SpacePostModerated
    from socialhome.domain.post import Post, PostType

    author = await stack.provision_user("author-mod")
    post = Post(
        id="p-mod",
        author=author.user_id,
        type=PostType.TEXT,
        content="test",
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    await stack.bus.publish(
        SpacePostModerated(
            space_id="sp-1",
            post=post,
            moderated_by="admin",
        )
    )
    notifs = await stack.notif_repo.list(author.user_id, limit=10)
    assert any(n.type == "post_moderated" for n in notifs)


# ── Momentum (§Momentum) ──────────────────────────────────────────────


async def test_moment_reaction_notifies_author(stack):
    from socialhome.domain.events import MomentReactionChanged

    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    await stack.bus.publish(
        MomentReactionChanged(
            moment_id="m-1",
            reactor_user_id=b.user_id,
            author_user_id=a.user_id,
            emoji="🔥",
        )
    )
    notifs = await stack.notif_repo.list(a.user_id, limit=10)
    assert any(n.type == "moment_reacted" and "🔥" in n.title for n in notifs)
    # bob (the reactor) gets nothing.
    assert await stack.notif_repo.list(b.user_id, limit=10) == []


async def test_moment_self_react_silent(stack):
    from socialhome.domain.events import MomentReactionChanged

    a = await stack.provision_user("anna")
    await stack.bus.publish(
        MomentReactionChanged(
            moment_id="m-1",
            reactor_user_id=a.user_id,
            author_user_id=a.user_id,
            emoji="❤️",
        )
    )
    assert await stack.notif_repo.list(a.user_id, limit=10) == []


async def test_moment_clear_reaction_silent(stack):
    """Clearing your reaction shouldn't ping the author again."""
    from socialhome.domain.events import MomentReactionChanged

    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    await stack.bus.publish(
        MomentReactionChanged(
            moment_id="m-1",
            reactor_user_id=b.user_id,
            author_user_id=a.user_id,
            emoji=None,
        )
    )
    assert await stack.notif_repo.list(a.user_id, limit=10) == []


async def test_moment_reply_notifies_parent_author(stack):
    from socialhome.domain.events import MomentCreated

    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    # Top-level moment from anna — does NOT notify anyone.
    await stack.bus.publish(
        MomentCreated(
            moment_id="m-root",
            author_user_id=a.user_id,
            content="root",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id=None,
            parent_author_user_id=None,
            origin_instance_id="self",
            expires_at="2026-12-01T00:00:00+00:00",
        )
    )
    assert await stack.notif_repo.list(a.user_id, limit=10) == []
    # bob replies — anna gets the ping.
    await stack.bus.publish(
        MomentCreated(
            moment_id="m-reply",
            author_user_id=b.user_id,
            content="hey",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id="m-root",
            parent_author_user_id=a.user_id,
            origin_instance_id="self",
            expires_at="2026-12-01T00:00:00+00:00",
        )
    )
    notifs = await stack.notif_repo.list(a.user_id, limit=10)
    assert any(n.type == "moment_replied" for n in notifs)


async def test_user_followed_notifies_recipient(stack):
    from socialhome.domain.events import UserFollowed

    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    await stack.bus.publish(
        UserFollowed(
            follower_user_id=b.user_id,
            followed_user_id=a.user_id,
        )
    )
    notifs = await stack.notif_repo.list(a.user_id, limit=10)
    assert any(n.type == "user_followed" for n in notifs)


async def test_user_self_follow_silent(stack):
    """Belt + braces — service-layer self-follow check is in user_service.
    The notification handler also short-circuits on self."""
    from socialhome.domain.events import UserFollowed

    a = await stack.provision_user("anna")
    await stack.bus.publish(
        UserFollowed(
            follower_user_id=a.user_id,
            followed_user_id=a.user_id,
        )
    )
    assert await stack.notif_repo.list(a.user_id, limit=10) == []


async def test_moment_reaction_remote_author_silent(stack):
    """Reactions for an author who lives on a peer instance get
    notified on the *peer's* side, not on this instance."""
    from socialhome.domain.events import MomentReactionChanged

    b = await stack.provision_user("bob")
    await stack.bus.publish(
        MomentReactionChanged(
            moment_id="m-1",
            reactor_user_id=b.user_id,
            author_user_id="uid-remote",  # lives on a peer
            emoji="🔥",
        )
    )
    # No local user has an unfilled bell — the reactor (bob) doesn't
    # get a self-ping, and the remote author has no local row.
    assert await stack.notif_repo.list(b.user_id, limit=10) == []


async def test_moment_top_level_no_notification(stack):
    """Top-level posts don't fire a notification — broadcast only."""
    from socialhome.domain.events import MomentCreated

    a = await stack.provision_user("anna")
    await stack.bus.publish(
        MomentCreated(
            moment_id="m-1",
            author_user_id=a.user_id,
            content="hi",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id=None,
            parent_author_user_id=None,
            origin_instance_id="self",
            expires_at="2026-12-01T00:00:00+00:00",
        )
    )
    assert await stack.notif_repo.list(a.user_id, limit=10) == []


async def test_moment_self_reply_silent(stack):
    """Replying to your own thread shouldn't ping yourself."""
    from socialhome.domain.events import MomentCreated

    a = await stack.provision_user("anna")
    await stack.bus.publish(
        MomentCreated(
            moment_id="m-2",
            author_user_id=a.user_id,
            content="self reply",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id="m-root",
            parent_author_user_id=a.user_id,
            origin_instance_id="self",
            expires_at="2026-12-01T00:00:00+00:00",
        )
    )
    assert await stack.notif_repo.list(a.user_id, limit=10) == []


async def test_moment_reply_remote_parent_silent(stack):
    """Reply to a parent whose author lives on a peer — no local notif."""
    from socialhome.domain.events import MomentCreated

    b = await stack.provision_user("bob")
    await stack.bus.publish(
        MomentCreated(
            moment_id="m-3",
            author_user_id=b.user_id,
            content="hey",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id="m-root",
            parent_author_user_id="uid-remote",
            origin_instance_id="self",
            expires_at="2026-12-01T00:00:00+00:00",
        )
    )
    assert await stack.notif_repo.list(b.user_id, limit=10) == []


async def test_moment_reply_without_parent_author_silent(stack):
    """Defensive: empty parent_author_user_id short-circuits the handler."""
    from socialhome.domain.events import MomentCreated

    b = await stack.provision_user("bob")
    await stack.bus.publish(
        MomentCreated(
            moment_id="m-4",
            author_user_id=b.user_id,
            content="hey",
            media_url=None,
            media_type=None,
            duration_ms=None,
            parent_moment_id="m-root",
            parent_author_user_id=None,
            origin_instance_id="self",
            expires_at="2026-12-01T00:00:00+00:00",
        )
    )
    assert await stack.notif_repo.list(b.user_id, limit=10) == []


async def test_user_followed_remote_recipient_silent(stack):
    """Following a remote user fires no local notification — the
    notification belongs to the followed user's home instance."""
    from socialhome.domain.events import UserFollowed

    a = await stack.provision_user("anna")
    await stack.bus.publish(
        UserFollowed(
            follower_user_id=a.user_id,
            followed_user_id="uid-remote",  # not local
        )
    )
    assert await stack.notif_repo.list(a.user_id, limit=10) == []


async def test_space_location_feature_enabled_notifies_non_actor_members(stack):
    """Enabling feature_location creates a notification for every member
    except the actor who flipped the toggle."""
    from socialhome.domain.events import SpaceLocationFeatureEnabled
    from socialhome.services.space_service import SpaceService
    from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
    from socialhome.crypto import derive_instance_id, generate_identity_keypair

    # Need a space service to create a proper space with members.
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)

    space_repo = _space_repo(stack.db)
    space_post_repo = SqliteSpacePostRepo(stack.db)
    space_svc = SpaceService(
        space_repo,
        space_post_repo,
        stack.notif_svc._users,
        stack.bus,
        own_instance_id=iid,
    )

    anna = await stack.provision_user("anna", is_admin=True)
    bob = await stack.provision_user("bob")
    carol = await stack.provision_user("carol")

    space = await space_svc.create_space(owner_username="anna", name="FamSpace")
    await space_svc.add_member(space.id, actor_username="anna", user_id=bob.user_id)
    await space_svc.add_member(space.id, actor_username="anna", user_id=carol.user_id)

    await stack.bus.publish(
        SpaceLocationFeatureEnabled(
            space_id=space.id,
            space_name="FamSpace",
            actor_user_id=anna.user_id,
        )
    )

    # Actor (anna) should NOT get a notification.
    anna_notifs = await stack.notif_repo.list(anna.user_id, limit=10)
    assert all(n.type != "space_location_enabled" for n in anna_notifs)

    # bob and carol SHOULD each get one.
    bob_notifs = await stack.notif_repo.list(bob.user_id, limit=10)
    assert any(n.type == "space_location_enabled" for n in bob_notifs)

    carol_notifs = await stack.notif_repo.list(carol.user_id, limit=10)
    assert any(n.type == "space_location_enabled" for n in carol_notifs)

    # Verify the notification body points at the settings page.
    loc_notif = next(n for n in bob_notifs if n.type == "space_location_enabled")
    assert "FamSpace" in loc_notif.title
    assert loc_notif.link_url == "/settings#privacy"


# ─── AppChallengeReceived handler (Task 7) ─────────────────────────────────


async def test_on_app_challenge_received_creates_row_for_target(stack):
    """An app challenge raises a bell row for the target, titled with the
    challenger's display name."""
    from socialhome.domain.events import AppChallengeReceived

    target = await stack.provision_user("target")
    await stack.bus.publish(
        AppChallengeReceived(
            app_id="chess",
            session_id="sess-xyz",
            to_user_id=target.user_id,
            from_display="Magnus",
        )
    )
    notifs = await stack.notif_repo.list(target.user_id, limit=10)
    rows = [n for n in notifs if n.type == "app_challenge"]
    assert len(rows) == 1
    assert rows[0].user_id == target.user_id
    assert "Magnus" in rows[0].title


async def test_on_app_challenge_received_push_is_title_only(stack):
    """The challenge push carries only a title — no body / UGC payload."""
    from socialhome.domain.events import AppChallengeReceived

    target = await stack.provision_user("target")
    push = _CapturingPush()
    stack.notif_svc.attach_push_service(push)
    await stack.bus.publish(
        AppChallengeReceived(
            app_id="chess",
            session_id="sess-xyz",
            to_user_id=target.user_id,
            from_display="Magnus",
        )
    )
    assert push.calls
    payload = push.calls[-1][1]
    assert "Magnus" in payload.title
    # PushPayload has no body field — title-only is structural (§25.3).
    assert not hasattr(payload, "body")


async def test_space_join_approved_for_a_non_local_user_saves_nothing(stack):
    """A §D2 cross-household applicant — or an admin/mod elevation of one —
    is approved with a ``user_id`` that has no local ``users`` row. The
    handler must not try to save a notification for it (the ``notifications``
    FK to ``users`` would fail and the whole SpaceJoinApproved handler would
    crash). It notifies the applicant over federation instead.
    """
    from socialhome.domain.events import SpaceJoinApproved
    from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
    from socialhome.services.space_service import SpaceService

    await stack.provision_user("anna")
    space_repo = _space_repo(stack.db)
    space_svc = SpaceService(
        space_repo,
        SqliteSpacePostRepo(stack.db),
        SqliteUserRepo(stack.db),
        stack.bus,
        own_instance_id="iid",
    )
    space = await space_svc.create_space(owner_username="anna", name="S")

    # Call the handler DIRECTLY (the bus swallows handler exceptions, which
    # would hide the crash): it must return cleanly, not raise the users-FK
    # IntegrityError, and save nothing for the non-local user.
    await stack.notif_svc.on_space_join_approved(
        SpaceJoinApproved(
            space_id=space.id,
            user_id="remote-user-not-local",
            request_id="req-1",
            approved_by="uid-anna",
        )
    )
    rows = await stack.notif_repo.list("remote-user-not-local", limit=50)
    assert rows == []


async def test_space_mention_from_remote_author_names_them(stack):
    """A federated post by a remote member names that member, not
    "Someone" (the author lives in ``remote_users``)."""
    space_svc, space, u = await _mention_space(stack, "bob")
    await stack.db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source) VALUES('peer-r', 'peer-r', ?, 'k1',"
        " 'k2', 'https://peer-r/wh', 'wh-peer-r', 'confirmed', 'manual')",
        ("00" * 32,),
    )
    await SqliteUserRepo(stack.db).upsert_remote(
        RemoteUser(
            user_id="r-zoe",
            instance_id="peer-r",
            remote_username="zoe",
            display_name="Zoe Remote",
        )
    )
    await stack.bus.publish(
        SpacePostCreated(
            post=Post(
                id="p-remote",
                author="r-zoe",
                type=PostType.TEXT,
                content="@bob hi",
                created_at=datetime.now(timezone.utc),
            ),
            space_id=space.id,
            mentions=(
                Mention(type=MentionType.USER, raw="@bob", user_id=u["bob"].user_id),
            ),
            origin_instance_id="peer-r",
        )
    )
    notes = [
        n
        for n in await stack.notif_repo.list(u["bob"].user_id, limit=50)
        if n.type == "space_mention"
    ]
    assert [n.title for n in notes] == ["Zoe Remote mentioned you in M"]


# ─── @here ───────────────────────────────────────────────────────────────

_HERE_TYPES = _CONTENT_TYPES | {"space_here"}


async def _here_types(stack, user):
    notes = await stack.notif_repo.list(user.user_id, limit=50)
    return [n.type for n in reversed(notes) if n.type in _HERE_TYPES]


async def _here_space(stack, *, allow=True, levels=None):
    space_svc, space, u = await _mention_space(
        stack, "bob", "carl", "erin", "dan", levels=levels
    )
    await space_svc.update_config(
        space.id, actor_username="anna", allow_here_mention=allow
    )
    return space_svc, space, u


async def _post(space_svc, space, author, content):
    return await space_svc.create_post(
        space.id, author_user_id=author.user_id, type=PostType.TEXT, content=content
    )


async def test_here_notifies_all_and_mentions_levels_not_muted_or_author(stack):
    space_svc, space, u = await _here_space(
        stack, levels={"carl": "mentions", "erin": "muted"}
    )
    await _post(space_svc, space, u["anna"], "@here dinner is ready")
    notes = [
        n
        for n in await stack.notif_repo.list(u["bob"].user_id, limit=50)
        if n.type == "space_here"
    ]
    assert [n.title for n in notes] == ["anna notified everyone in M"]
    assert await _here_types(stack, u["carl"]) == ["space_here"]  # mentions level
    assert await _here_types(stack, u["erin"]) == []  # muted
    assert await _here_types(stack, u["anna"]) == []  # author


async def test_here_and_direct_mention_is_one_bell(stack):
    """A member both @-mentioned and covered by @here gets the direct
    mention only — never two bells for one post."""
    space_svc, space, u = await _here_space(stack)
    await _post(space_svc, space, u["anna"], "@here and especially @bob")
    assert await _here_types(stack, u["bob"]) == ["space_mention"]
    assert await _here_types(stack, u["carl"]) == ["space_here"]


async def test_here_push_is_title_only(stack):
    space_svc, space, u = await _here_space(stack)
    sent = []

    class _Push:
        async def push_to_user(self, user_id, payload):
            sent.append((user_id, payload))

    stack.notif_svc.attach_push_service(_Push())
    await _post(space_svc, space, u["anna"], "@here secret plans")
    assert sent and all(p.title == "anna notified everyone in M" for _, p in sent)
    assert all("secret" not in p.to_json() for _, p in sent)


async def test_here_from_plain_member_is_ignored(stack):
    """Owner/admin only: a member's @here is dropped at resolve time and the
    post notifies exactly as if it had no @here."""
    space_svc, space, u = await _here_space(stack, levels={"carl": "mentions"})
    await _post(space_svc, space, u["bob"], "@here look at this")
    assert await _here_types(stack, u["carl"]) == []
    assert await _here_types(stack, u["dan"]) == ["space_post_created"]


async def test_here_ignored_when_space_toggle_off(stack):
    space_svc, space, u = await _here_space(
        stack, allow=False, levels={"carl": "mentions"}
    )
    await _post(space_svc, space, u["anna"], "@here anyone?")
    assert await _here_types(stack, u["carl"]) == []
    assert await _here_types(stack, u["bob"]) == ["space_post_created"]


async def test_here_is_rate_limited_per_author_per_space(stack):
    """One @here per author per space per 10 min: a second one inside the
    window notifies like a plain post; after the window it pages again."""
    now = [1000.0]
    stack.notif_svc._clock = lambda: now[0]
    space_svc, space, u = await _here_space(stack, levels={"carl": "mentions"})
    await _post(space_svc, space, u["anna"], "@here one")
    await _post(space_svc, space, u["anna"], "@here two")
    assert await _here_types(stack, u["bob"]) == ["space_here", "space_post_created"]
    assert await _here_types(stack, u["carl"]) == ["space_here"]
    now[0] += 601
    await _post(space_svc, space, u["anna"], "@here three")
    assert await _here_types(stack, u["carl"]) == ["space_here", "space_here"]


async def test_here_in_comment_notifies_members(stack):
    space_svc, space, u = await _here_space(stack, levels={"carl": "mentions"})
    post = await _post(space_svc, space, u["bob"], "plain")
    await space_svc.add_comment(
        post.id, author_user_id=u["anna"].user_id, content="@here see above"
    )
    notes = [
        n
        for n in await stack.notif_repo.list(u["carl"].user_id, limit=50)
        if n.type in _HERE_TYPES
    ]
    assert [(n.type, n.title) for n in notes] == [
        ("space_here", "anna notified everyone in M")
    ]


# ─── Edits that add a mention ────────────────────────────────────────────


async def test_space_post_edit_notifies_only_newly_mentioned(stack):
    """Editing a post to add @carl rings carl (mention bell); bob, already
    mentioned, is not re-notified; nobody gets a generic bell for an edit."""
    space_svc, space, u = await _mention_space(
        stack, "bob", "carl", "dan", levels={"carl": "mentions"}
    )
    post = await _post(space_svc, space, u["anna"], "hi @bob")
    before = {k: await _types(stack, v) for k, v in u.items()}
    await space_svc.edit_post(
        post.id, editor_user_id=u["anna"].user_id, new_content="hi @bob and @carl"
    )
    assert await _types(stack, u["bob"]) == before["bob"] == ["space_mention"]
    assert await _types(stack, u["carl"]) == ["space_mention"]
    assert await _types(stack, u["dan"]) == before["dan"] == ["space_post_created"]
    # A second edit keeping both mentions notifies nobody again.
    await space_svc.edit_post(
        post.id, editor_user_id=u["anna"].user_id, new_content="hi @carl, @bob!"
    )
    assert await _types(stack, u["carl"]) == ["space_mention"]


async def test_space_post_edit_by_an_admin_never_notifies(stack):
    """A moderation edit of someone else's post isn't the author speaking —
    it adds nobody's mention."""
    space_svc, space, u = await _mention_space(stack, "bob", "carl")
    post = await _post(space_svc, space, u["bob"], "hello")
    await space_svc.edit_post(
        post.id, editor_user_id=u["anna"].user_id, new_content="hello @carl"
    )
    assert await _types(stack, u["carl"]) == ["space_post_created"]


async def test_space_comment_edit_notifies_only_newly_mentioned(stack):
    space_svc, space, u = await _mention_space(
        stack, "bob", "carl", levels={"carl": "muted"}
    )
    post = await _post(space_svc, space, u["anna"], "plain")
    c = await space_svc.add_comment(
        post.id, author_user_id=u["bob"].user_id, content="nice"
    )
    await space_svc.edit_comment(
        c.id, editor_user_id=u["bob"].user_id, new_content="nice @anna @carl"
    )
    notes = [
        n
        for n in await stack.notif_repo.list(u["anna"].user_id, limit=50)
        if n.type == "space_mention"
    ]
    assert [n.title for n in notes] == ["bob mentioned you in a comment in M"]
    assert "space_mention" not in await _types(stack, u["carl"])  # muted


async def test_here_added_on_edit_pages_once_and_stays_rate_limited(stack):
    """@here newly added on edit pages everyone (owner, toggle on); an edit
    that keeps it doesn't re-page; inside the 10-min window a fresh @here
    edit on another post pages nobody."""
    now = [1000.0]
    stack.notif_svc._clock = lambda: now[0]
    space_svc, space, u = await _here_space(stack, levels={"carl": "mentions"})
    p1 = await _post(space_svc, space, u["anna"], "dinner")
    await space_svc.edit_post(
        p1.id, editor_user_id=u["anna"].user_id, new_content="@here dinner"
    )
    assert await _here_types(stack, u["carl"]) == ["space_here"]
    await space_svc.edit_post(
        p1.id, editor_user_id=u["anna"].user_id, new_content="@here dinner now"
    )
    assert await _here_types(stack, u["carl"]) == ["space_here"]
    p2 = await _post(space_svc, space, u["anna"], "later")
    await space_svc.edit_post(
        p2.id, editor_user_id=u["anna"].user_id, new_content="@here later"
    )
    assert await _here_types(stack, u["carl"]) == ["space_here"]


async def test_edit_events_without_new_mentions_are_silent(stack):
    """Bare ``PostEdited`` / ``CommentUpdated`` (household feed, or no new
    mention) never notify."""
    space_svc, space, u = await _mention_space(stack, "bob")
    post = await _post(space_svc, space, u["anna"], "x")
    before = await _types(stack, u["bob"])
    await stack.bus.publish(PostEdited(post=post, space_id=space.id))
    await stack.bus.publish(PostEdited(post=post, new_mentions=(_m(u["bob"]),)))
    c = Comment(
        id="c-x",
        post_id=post.id,
        author=u["anna"].user_id,
        type=CommentType.TEXT,
        created_at=datetime.now(timezone.utc),
        content="y",
    )
    await stack.bus.publish(CommentUpdated(post_id=post.id, comment=c))
    assert await _types(stack, u["bob"]) == before


class _BlockingCP:
    """Child-protection stand-in: a guardian block between two users."""

    def __init__(self, a: str, b: str) -> None:
        self._pairs = {a: frozenset({b}), b: frozenset({a})}

    async def guardian_block_counterparts(self, user_id: str) -> frozenset[str]:
        return self._pairs.get(user_id, frozenset())

    async def is_restricted(self, user_id, capability) -> bool:
        return False

    def register_gate(self, gate) -> None:
        pass


async def test_dm_edit_mention_across_a_guardian_block_is_silent(stack):
    """A guardian block between sender and a member also stops the mention
    bell an edit would add; other newly mentioned members still get it."""
    anna = await stack.provision_user("anna-gb")
    kid = await stack.provision_user("kid-gb")
    carl = await stack.provision_user("carl-gb")
    await _named_group(stack, "g-gb", "G", anna, kid, carl)
    stack.notif_svc.attach_child_protection(_BlockingCP(anna.user_id, kid.user_id))
    await stack.bus.publish(
        DmMessageUpdated(
            conversation_id="g-gb",
            message_id="m-1",
            sender_user_id=anna.user_id,
            recipient_user_ids=(kid.user_id, carl.user_id),
            content="@kid-gb @carl-gb",
            edited_at=datetime.now(timezone.utc),
            new_mentions=(_m(kid), _m(carl)),
            sender_display_name="Anna",
        )
    )
    assert await stack.notif_repo.list(kid.user_id) == []
    assert [n.type for n in await stack.notif_repo.list(carl.user_id)] == ["dm_mention"]
