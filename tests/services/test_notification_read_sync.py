"""Reading something clears its notification on every device.

Covers :class:`NotificationService`'s read side: the DM auto-clear tells
every device, a bumped row neither counts nor buzzes twice, visiting a page
reads the rows that open it, and an action item one admin handled reads as
read for every admin.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import (
    CallRingStopped,
    DmMessageCreated,
    NotificationCreated,
    NotificationReadChanged,
    ReportResolved,
    SpaceJoinApproved,
    SpaceJoinDenied,
    SpaceModerationApproved,
)
from socialhome.domain.notification import NotificationObject, NotificationSubject
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
from socialhome.services.push_withdraw_service import PushWithdrawService
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

    async def get(self, report_id: str):
        return None  # the reporter's row is covered in test_notification_calls


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


async def _row(env, user_id: str, *, type: str, link: str, key: str | None = None):
    note = replace(
        new_notification(user_id=user_id, type=type, title=type, link_url=link),
        object_key=key,
    )
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
    bob = await env.user("bob")
    await _row(env, bob.user_id, type="dm_mention", link="/dms/c1")
    await _row(env, bob.user_id, type="post_created", link="/post/p1")
    env.read_changed.clear()

    assert await env.svc.mark_read_for_dm(bob.user_id, "c1") == 1

    assert [(e.user_id, e.unread_count) for e in env.read_changed] == [(bob.user_id, 1)]


async def test_opening_a_dm_thread_with_nothing_unread_stays_quiet(env):
    bob = await env.user("bob")
    assert await env.svc.mark_read_for_dm(bob.user_id, "c1") == 0
    assert env.read_changed == []


# ── 2. A bumped row counts, buzzes and rings HA once ──────────────────


async def _mention(env, sender, recipient, conv: str = "c1"):
    """A ``dm_mention`` row through the gate, as the DM handler writes it."""
    await env.svc._save_notif(
        new_notification(
            user_id=recipient.user_id,
            type="dm_mention",
            title="mention",
            link_url=f"/dms/{conv}",
        ),
        about=NotificationSubject.of(NotificationObject.CONVERSATION, conv),
        actor_user_id=sender.user_id,
    )


async def test_a_mention_burst_pushes_once_and_says_it_bumped(env):
    """D8: each repeat on the one unread row stays silent (no second buzz),
    and its ``NotificationCreated`` says it bumped (no badge +1)."""
    anna, bob = await env.user("anna"), await env.user("bob")
    for _ in range(3):
        await _mention(env, anna, bob)

    assert await env.notifs.count_unread(bob.user_id) == 1
    assert len(env.push.sent) == 1
    assert [e.bumped for e in env.created] == [False, True, True]
    assert [e.silent for e in env.created] == [False, True, True]
    assert len({e.notification_id for e in env.created}) == 1


async def test_a_mention_after_reading_reopens_the_row_and_pushes(env):
    """D7 + D8: the conversation keeps one row; a mention after reading
    re-opens it (unread again, to the top), pushes like a new one, and
    every device hears the new count."""
    anna, bob = await env.user("anna"), await env.user("bob")
    await _mention(env, anna, bob)
    await env.svc.mark_read_for_dm(bob.user_id, "c1")
    env.read_changed.clear()
    await _mention(env, anna, bob)
    assert [(e.bumped, e.reopened, e.silent) for e in env.created] == [
        (False, False, False),
        (True, True, False),
    ]
    assert env.created[0].notification_id == env.created[1].notification_id
    assert len(env.push.sent) == 2
    assert [(e.user_id, e.unread_count) for e in env.read_changed] == [(bob.user_id, 1)]


async def test_a_chat_message_makes_no_row(env):
    """D3: plain chat messages push but never land in Notifications."""
    anna, bob = await env.user("anna"), await env.user("bob")
    await env.bus.publish(_dm(anna, bob))
    assert await env.notifs.count_unread(bob.user_id) == 0
    assert env.created == []
    assert [u for u, _ in env.push.sent] == [bob.user_id]


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


async def test_a_handled_join_request_clears_it_for_every_admin(env):
    """Admin A approved the request; admin B's "wants to join" row stayed
    unread forever."""
    a, b = await env.user("adminA"), await env.user("adminB")
    link = "/spaces/s1/moderation"
    for admin in (a, b):
        await _row(
            env,
            admin.user_id,
            type="space_join_requested",
            link=link,
            key="join_request:r1",
        )
    await _row(env, b.user_id, type="moderation_pending", link=link, key="space:s1")

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


async def test_a_handled_request_leaves_the_others_waiting(env):
    """One row per request: answering r1 never touches r2's row — and an
    older space-wide row stays while any request waits."""
    a = await env.user("adminA")
    link = "/spaces/s1/moderation"
    await _row(
        env, a.user_id, type="space_join_requested", link=link, key="join_request:r2"
    )
    await _row(env, a.user_id, type="space_join_requested", link=link, key="space:s1")
    env.spaces.join_requests["s1"] = [{"id": "r2"}]
    await env.bus.publish(
        SpaceJoinDenied(space_id="s1", user_id="x", request_id="r1", denied_by="z")
    )
    assert await env.notifs.count_unread(a.user_id) == 2
    assert env.read_changed == []

    env.spaces.join_requests["s1"] = []
    await env.bus.publish(
        SpaceJoinDenied(space_id="s1", user_id="x", request_id="r2", denied_by="z")
    )
    assert await env.notifs.count_unread(a.user_id) == 0


async def test_the_review_queue_emptied_clears_pending_review_rows(env):
    a, b = await env.user("modA"), await env.user("modB")
    for mod in (a, b):
        await _row(
            env,
            mod.user_id,
            type="moderation_pending",
            link="/spaces/s1/moderation",
            key="space:s1",
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
    await _row(
        env,
        a.user_id,
        type="space_report",
        link="/spaces/s1/moderation",
        key="space:s1",
    )
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


# ── 5. What was read or handled comes off the devices ─────────────────


class _Withdrawals:
    """Records what the service asks :class:`PushWithdrawService` to do."""

    def __init__(self) -> None:
        self.pushed: list[tuple[str, str]] = []
        self.withdrawn: list[tuple[str, list[str]]] = []

    def note_pushed(self, user_id: str, tag: str) -> None:
        self.pushed.append((user_id, tag))

    def withdraw(self, user_id: str, tags) -> list[str]:
        self.withdrawn.append((user_id, list(tags)))
        return list(tags)


@pytest.fixture
def withdrawals(env) -> _Withdrawals:
    w = _Withdrawals()
    env.svc.attach_push_withdraw(w)
    return w


async def test_every_push_is_remembered_by_its_tag(env, withdrawals):
    anna, bob = await env.user("anna"), await env.user("bob")
    await _mention(env, anna, bob)
    await env.bus.publish(_dm(anna, bob, conv="c2"))
    assert withdrawals.pushed == [
        (bob.user_id, "dm_mention:/dms/c1"),
        (bob.user_id, "dm_message:/dms/c2"),
    ]


async def test_reading_one_row_withdraws_its_push(env, withdrawals):
    bob, cara = await env.user("bob"), await env.user("cara")
    note = await _row(env, bob.user_id, type="post_created", link="/post/p1")
    await env.svc.mark_read(bob.user_id, note.id)
    assert withdrawals.withdrawn == [(bob.user_id, ["post_created:/post/p1"])]
    assert await env.notifs.count_unread(bob.user_id) == 0
    assert [(e.user_id, e.unread_count) for e in env.read_changed] == [(bob.user_id, 0)]
    # Already read, someone else's or unknown: nothing to withdraw.
    await env.svc.mark_read(bob.user_id, note.id)
    await env.svc.mark_read(cara.user_id, note.id)
    await env.svc.mark_read(bob.user_id, "nope")
    assert len(withdrawals.withdrawn) == 1


async def test_a_row_without_a_link_withdraws_by_its_id(env, withdrawals):
    bob = await env.user("bob")
    note = new_notification(user_id=bob.user_id, type="poll_result", title="t")
    await env.notifs.save(note)
    await env.svc.mark_read(bob.user_id, note.id)
    assert withdrawals.withdrawn == [(bob.user_id, [f"poll_result:{note.id}"])]


async def test_read_all_withdraws_every_unread_rows_push(env, withdrawals):
    bob = await env.user("bob")
    await _row(env, bob.user_id, type="post_created", link="/post/p1")
    await _row(env, bob.user_id, type="task_assigned", link="/tasks/t1")
    done = await _row(env, bob.user_id, type="post_created", link="/post/p0")
    await env.notifs.mark_read(done.id, bob.user_id)
    await env.svc.mark_all_read(bob.user_id)
    [(user_id, tags)] = withdrawals.withdrawn
    assert user_id == bob.user_id
    assert sorted(tags) == ["post_created:/post/p1", "task_assigned:/tasks/t1"]
    assert await env.notifs.count_unread(bob.user_id) == 0
    assert [(e.user_id, e.unread_count) for e in env.read_changed] == [(bob.user_id, 0)]


async def test_reading_by_link_withdraws_what_flipped(env, withdrawals):
    bob = await env.user("bob")
    await _row(env, bob.user_id, type="post_created", link="/post/p1")
    await _row(env, bob.user_id, type="comment_added", link="/post/p1?comment=c9")
    await _row(env, bob.user_id, type="post_created", link="/post/p2")
    await env.svc.mark_read_by_links(bob.user_id, ["/post/p1"])
    [(_, tags)] = withdrawals.withdrawn
    assert sorted(tags) == [
        "comment_added:/post/p1?comment=c9",
        "post_created:/post/p1",
    ]
    # Nothing flipped: nothing to withdraw.
    await env.svc.mark_read_by_links(bob.user_id, ["/post/p1"])
    assert len(withdrawals.withdrawn) == 1


async def test_reading_by_object_withdraws_what_flipped(env, withdrawals):
    bob = await env.user("bob")
    await _row(env, bob.user_id, type="post_reacted", link="/post/p1", key="post:p1")
    await _row(env, bob.user_id, type="post_created", link="/post/p2", key="post:p2")
    assert await env.svc.mark_read_by_objects(bob.user_id, ["post:p1"]) == 1
    assert withdrawals.withdrawn == [(bob.user_id, ["post_reacted:/post/p1"])]
    assert await env.svc.mark_read_by_objects(bob.user_id, ["post:p1"]) == 0
    assert len(withdrawals.withdrawn) == 1


async def test_opening_a_chat_withdraws_its_message_mention_and_call_pushes(
    env, withdrawals
):
    """A chat message has no row, yet its push must still go: opening the
    chat withdraws every tag the chat's pushes carry."""
    bob = await env.user("bob")
    await env.svc.mark_read_for_dm(bob.user_id, "c1")
    assert withdrawals.withdrawn == [
        (
            bob.user_id,
            [
                "dm_message:/dms/c1",
                "dm_mention:/dms/c1",
                "bazaar_question:/dms/c1",
                "call_missed:/dms/c1",
                "chat_added:/dms/c1",
            ],
        )
    ]


async def test_a_handled_request_withdraws_it_from_every_admin(env, withdrawals):
    a, b = await env.user("adminA"), await env.user("adminB")
    link = "/spaces/s1/moderation"
    for admin in (a, b):
        await _row(
            env,
            admin.user_id,
            type="space_join_requested",
            link=link,
            key="join_request:r1",
        )
    await _row(env, b.user_id, type="moderation_pending", link=link, key="space:s1")
    await env.bus.publish(
        SpaceJoinApproved(
            space_id="s1", user_id="x", request_id="r1", approved_by=a.user_id
        )
    )
    assert sorted(withdrawals.withdrawn) == sorted(
        [
            (a.user_id, [f"space_join_requested:{link}"]),
            (b.user_id, [f"space_join_requested:{link}"]),
        ]
    )


async def test_an_answered_call_withdraws_the_ring_not_a_missed_call(env, withdrawals):
    """The ring shares ``call_missed:<link>`` with the missed call; an
    answered or declined call takes it off the device instead."""
    bob, cara = await env.user("bob"), await env.user("cara")
    await env.bus.publish(
        CallRingStopped(
            call_id="k1", conversation_id="c1", user_ids=(bob.user_id, cara.user_id)
        )
    )
    assert withdrawals.withdrawn == [
        (bob.user_id, ["call_missed:/dms/c1"]),
        (cara.user_id, ["call_missed:/dms/c1"]),
    ]
    assert env.created == []  # no missed-call row


async def test_without_a_withdraw_service_reads_still_work(env):
    bob = await env.user("bob")
    await _row(env, bob.user_id, type="post_reacted", link="/post/p1", key="post:p1")
    await env.bus.publish(
        CallRingStopped(call_id="k1", conversation_id="c1", user_ids=(bob.user_id,))
    )
    assert await env.svc.mark_read_by_objects(bob.user_id, ["post:p1"]) == 1
    await env.svc.mark_all_read(bob.user_id)
    await env.svc.mark_read_for_dm(bob.user_id, "c1")


async def test_a_pushed_chat_message_comes_off_when_read_elsewhere(env):
    """End to end with the real :class:`PushWithdrawService`: Anna's message
    buzzed Bob's phone; Bob opens the chat on his laptop; one withdraw push
    with the message's tag goes to the phone."""
    anna, bob = await env.user("anna"), await env.user("bob")
    web = _WebWithdraw()
    service = PushWithdrawService(env.svc._users)
    service.attach_push_service(web)
    env.svc.attach_push_withdraw(service)

    await env.bus.publish(_dm(anna, bob))
    await env.svc.mark_read_for_dm(bob.user_id, "c1")
    await env.svc.mark_read_for_dm(bob.user_id, "c1")  # nothing left: no-op
    await service.flush(bob.user_id)

    assert web.calls == [(bob.user_id, ["dm_message:/dms/c1"])]


class _WebWithdraw:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    async def withdraw_from_user(self, user_id: str, tags: list[str]) -> int:
        self.calls.append((user_id, tags))
        return 1
