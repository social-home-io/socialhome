"""Reading something clears its notification on every device.

Covers :class:`NotificationService`'s read side: the DM auto-clear tells
every device, a bumped row neither counts nor buzzes twice, visiting a page
reads the rows that open it, and an action item one admin handled reads as
read for every admin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import (
    DmMessageCreated,
    NotificationCreated,
    NotificationReadChanged,
    ReportResolved,
    SpaceJoinApproved,
    SpaceJoinDenied,
    SpaceModerationApproved,
)
from socialhome.domain.space import (
    ModerationStatus,
    SpaceModerationItem,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.notification_repo import (
    SqliteNotificationRepo,
    new_notification,
)
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.notification_service import NotificationService
from socialhome.services.user_service import UserService


@dataclass
class _Spaces:
    """Just the queue reads the resolve handlers make."""

    join_requests: dict[str, list[dict]] = field(default_factory=dict)
    pending_items: dict[str, int] = field(default_factory=dict)

    async def list_pending_join_requests(self, space_id: str) -> list[dict]:
        return self.join_requests.get(space_id, [])

    async def count_pending(self, space_id: str, *, submitted_by=None) -> int:
        return self.pending_items.get(space_id, 0)


@dataclass
class _Reports:
    pending: dict[str, int] = field(default_factory=dict)

    async def count_pending_in_space(self, space_id: str, **_kw) -> int:
        return self.pending.get(space_id, 0)


class _Push:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def push_to_user(self, user_id, payload) -> None:
        self.sent.append((user_id, payload))


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        """INSERT INTO instance_identity(instance_id, identity_private_key,
           identity_public_key, routing_secret) VALUES(?,?,?,?)""",
        (
            derive_instance_id(kp.public_key),
            kp.private_key.hex(),
            kp.public_key.hex(),
            "aa" * 32,
        ),
    )
    bus = EventBus()
    users = SqliteUserRepo(db)
    notifs = SqliteNotificationRepo(db, max_per_user=50)
    spaces = _Spaces()
    reports = _Reports()
    svc = NotificationService(notifs, users, spaces, bus)
    svc.attach_report_repo(reports)
    push = _Push()
    svc.attach_push_service(push)
    svc.wire()
    user_svc = UserService(users, bus, own_instance_public_key=kp.public_key)

    class Env:
        pass

    e = Env()
    e.bus, e.svc, e.notifs, e.spaces, e.reports, e.push = (
        bus,
        svc,
        notifs,
        spaces,
        reports,
        push,
    )
    e.read_changed = []
    e.created = []

    async def _rc(ev: NotificationReadChanged) -> None:
        e.read_changed.append(ev)

    async def _nc(ev: NotificationCreated) -> None:
        e.created.append(ev)

    bus.subscribe(NotificationReadChanged, _rc)
    bus.subscribe(NotificationCreated, _nc)

    async def user(name: str):
        return await user_svc.provision(username=name, display_name=name)

    e.user = user
    yield e
    await db.shutdown()


async def _row(env, user_id: str, *, type: str, link: str):
    note = new_notification(user_id=user_id, type=type, title=type, link_url=link)
    await env.notifs.save(note)
    return note


def _dm(sender, recipient, conv: str = "c1") -> DmMessageCreated:
    return DmMessageCreated(
        conversation_id=conv,
        message_id="m",
        sender_user_id=sender.user_id,
        sender_display_name=sender.display_name,
        recipient_user_ids=(recipient.user_id,),
    )


# ── 1. The DM auto-clear tells every device ───────────────────────────


async def test_opening_a_dm_thread_updates_the_badge_on_every_device(env):
    """``mark_read_for_dm`` flipped the rows but published nothing, so
    every other device kept the stale badge until its 30-s poll."""
    anna, bob = await env.user("anna"), await env.user("bob")
    await env.bus.publish(_dm(anna, bob))
    await _row(env, bob.user_id, type="post_created", link="/post/p1")
    env.read_changed.clear()

    assert await env.svc.mark_read_for_dm(bob.user_id, "c1") == 1

    assert [(e.user_id, e.unread_count) for e in env.read_changed] == [(bob.user_id, 1)]


async def test_opening_a_dm_thread_with_nothing_unread_stays_quiet(env):
    bob = await env.user("bob")
    assert await env.svc.mark_read_for_dm(bob.user_id, "c1") == 0
    assert env.read_changed == []


# ── 2. A bumped row counts, buzzes and rings HA once ──────────────────


async def test_a_dm_burst_pushes_once_and_says_it_bumped(env):
    """Each message of a burst bumped the one unread row and pushed again,
    and its ``NotificationCreated`` looked new (badge +1 per message)."""
    anna, bob = await env.user("anna"), await env.user("bob")
    for _ in range(3):
        await env.bus.publish(_dm(anna, bob))

    assert await env.notifs.count_unread(bob.user_id) == 1
    assert len(env.push.sent) == 1
    assert [e.bumped for e in env.created] == [False, True, True]
    assert len({e.notification_id for e in env.created}) == 1


async def test_a_dm_after_reading_reopens_the_row_and_pushes(env):
    """D7: the conversation keeps one row; a message after reading re-opens
    it (unread again, to the top), pushes like a new one, and every device
    hears the new count."""
    anna, bob = await env.user("anna"), await env.user("bob")
    await env.bus.publish(_dm(anna, bob))
    await env.svc.mark_read_for_dm(bob.user_id, "c1")
    env.read_changed.clear()
    await env.bus.publish(_dm(anna, bob))
    assert [(e.bumped, e.reopened, e.silent) for e in env.created] == [
        (False, False, False),
        (True, True, False),
    ]
    assert env.created[0].notification_id == env.created[1].notification_id
    assert len(env.push.sent) == 2
    assert [(e.user_id, e.unread_count) for e in env.read_changed] == [(bob.user_id, 1)]


# ── 3. Visiting a page reads the rows that open it ────────────────────


async def test_viewing_a_post_reads_its_post_and_comment_rows(env):
    """Opening the post cleared nothing: its "posted" and "commented" rows
    stayed unread until clicked one by one."""
    bob = await env.user("bob")
    posted = await _row(env, bob.user_id, type="post_created", link="/post/p1")
    commented = await _row(
        env, bob.user_id, type="comment_added", link="/post/p1?comment=c9"
    )
    other = await _row(env, bob.user_id, type="post_created", link="/post/p2")

    assert await env.svc.mark_read_by_links(bob.user_id, ["/post/p1"]) == 2

    unread = {n.id for n in await env.notifs.list_unread(bob.user_id)}
    assert unread == {other.id}
    assert posted.id not in unread and commented.id not in unread
    assert [(e.user_id, e.unread_count) for e in env.read_changed] == [(bob.user_id, 1)]


async def test_viewing_a_space_feed_leaves_its_post_rows_alone(env):
    """The feed is not every post in it: only rows that open the feed itself
    are read; a post's row waits until that post is opened."""
    bob = await env.user("bob")
    joined = await _row(env, bob.user_id, type="space_member_joined", link="/spaces/s1")
    post = await _row(
        env, bob.user_id, type="space_post_created", link="/spaces/s1?post=p1"
    )
    comment = await _row(
        env,
        bob.user_id,
        type="space_comment_added",
        link="/spaces/s1?post=p1&comment=c1",
    )

    assert await env.svc.mark_read_by_links(bob.user_id, ["/spaces/s1"]) == 1
    unread = {n.id for n in await env.notifs.list_unread(bob.user_id)}
    assert unread == {post.id, comment.id}
    assert joined.id not in unread

    assert await env.svc.mark_read_by_links(bob.user_id, ["/spaces/s1?post=p1"]) == 2
    assert await env.notifs.count_unread(bob.user_id) == 0


async def test_viewing_a_moment_or_highlight_reads_its_rows(env):
    """Opening a moment through its "replied" row left the "reacted" row
    for the same moment unread."""
    bob = await env.user("bob")
    await _row(env, bob.user_id, type="moment_reacted", link="/momentum/m1")
    await _row(env, bob.user_id, type="moment_replied", link="/momentum/m1?reply=r1")
    await _row(env, bob.user_id, type="highlight_reacted", link="/highlights/h1")

    assert (
        await env.svc.mark_read_by_links(
            bob.user_id, ["/momentum/m1", "/highlights/h1"]
        )
        == 3
    )
    assert await env.notifs.count_unread(bob.user_id) == 0


async def test_a_visit_reads_only_the_viewers_rows(env):
    bob, cara = await env.user("bob"), await env.user("cara")
    await _row(env, cara.user_id, type="post_created", link="/post/p1")
    assert await env.svc.mark_read_by_links(bob.user_id, ["/post/p1"]) == 0
    assert await env.notifs.count_unread(cara.user_id) == 1
    assert env.read_changed == []


# ── 4. Handled by one admin, handled for all ──────────────────────────


async def test_the_last_join_request_handled_clears_it_for_every_admin(env):
    """Admin A approved the request; admin B's "wants to join" row stayed
    unread forever."""
    a, b = await env.user("adminA"), await env.user("adminB")
    link = "/spaces/s1/moderation"
    for admin in (a, b):
        await _row(env, admin.user_id, type="space_join_requested", link=link)
    await _row(env, b.user_id, type="moderation_pending", link=link)

    await env.bus.publish(
        SpaceJoinApproved(
            space_id="s1", user_id="x", request_id="r1", approved_by=a.user_id
        )
    )

    assert await env.notifs.count_unread(a.user_id) == 0
    # B's post review is another queue — still waiting.
    assert await env.notifs.count_unread(b.user_id) == 1
    assert sorted((e.user_id, e.unread_count) for e in env.read_changed) == sorted(
        [(a.user_id, 0), (b.user_id, 1)]
    )


async def test_a_join_request_handled_while_others_wait_keeps_the_rows(env):
    a = await env.user("adminA")
    await _row(
        env, a.user_id, type="space_join_requested", link="/spaces/s1/moderation"
    )
    env.spaces.join_requests["s1"] = [{"id": "r2"}]
    await env.bus.publish(
        SpaceJoinDenied(space_id="s1", user_id="x", request_id="r1", denied_by="z")
    )
    assert await env.notifs.count_unread(a.user_id) == 1
    assert env.read_changed == []


async def test_the_review_queue_emptied_clears_pending_review_rows(env):
    a, b = await env.user("modA"), await env.user("modB")
    for mod in (a, b):
        await _row(
            env, mod.user_id, type="moderation_pending", link="/spaces/s1/moderation"
        )
    item = SpaceModerationItem(
        id="i1",
        space_id="s1",
        feature="feed",
        action="post",
        submitted_by="x",
        payload={},
        current_snapshot=None,
        submitted_at=datetime(2026, 10, 27, tzinfo=UTC),
        expires_at=datetime(2026, 11, 3, tzinfo=UTC),
        status=ModerationStatus.APPROVED,
    )
    env.spaces.pending_items["s1"] = 1
    await env.bus.publish(SpaceModerationApproved(item=item))
    assert await env.notifs.count_unread(a.user_id) == 1  # one still waits

    env.spaces.pending_items["s1"] = 0
    await env.bus.publish(SpaceModerationApproved(item=item))
    assert await env.notifs.count_unread(a.user_id) == 0
    assert await env.notifs.count_unread(b.user_id) == 0


async def test_the_last_open_report_resolved_clears_report_rows(env):
    a = await env.user("modA")
    await _row(env, a.user_id, type="space_report", link="/spaces/s1/moderation")
    env.reports.pending["s1"] = 1
    await env.bus.publish(
        ReportResolved(report_id="r1", resolved_by="z", space_id="s1")
    )
    assert await env.notifs.count_unread(a.user_id) == 1

    env.reports.pending["s1"] = 0
    await env.bus.publish(
        ReportResolved(report_id="r2", resolved_by="z", space_id="s1")
    )
    assert await env.notifs.count_unread(a.user_id) == 0

    # A household-level report has no space rows to clear.
    await env.bus.publish(ReportResolved(report_id="r3", resolved_by="z"))
