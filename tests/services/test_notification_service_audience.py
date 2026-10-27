"""Notifications go to the right people, by name.

Regression tests for the audience rules every notification shares
(:meth:`NotificationService._save_notif` is the one gate):

* a recipient is always a local user — a remote or unknown id never gets a
  row (and never trips the ``notifications.user_id`` FK, which used to abort
  the rest of a fan-out);
* nobody hears about an actor they blocked, and a protected child never
  hears about an actor their guardian blocked — on every type, including
  the ones that skipped the check (tasks, calendar, joins, bazaar, app
  challenges, invite answers);
* remote people are named, never shown as a raw user id;
* invite answers go to the inviter only and name the person and the space;
* §CP.R: a protected account hears of a moment reaction from its circle
  only; ``space_location_enabled`` never reaches a follower or a protected
  account.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import UTC, date, datetime

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.calendar import CalendarEvent
from socialhome.domain.events import (
    AppChallengeReceived,
    BazaarBidPlaced,
    BazaarOfferAccepted,
    BazaarOfferRejected,
    CalendarEventCreated,
    MomentReactionChanged,
    RemoteSpaceInviteAccepted,
    RemoteSpaceInviteDeclined,
    SpaceJoinRequested,
    SpaceLocationFeatureEnabled,
    SpaceMemberJoined,
    SpacePostModerated,
    TaskAssigned,
    TaskCompleted,
    TaskDeadlineDue,
)
from socialhome.domain.post import Post, PostType
from socialhome.domain.task import Task, TaskStatus
from socialhome.domain.user import RemoteUser
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.notification_repo import SqliteNotificationRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services import notification_service as ns_module
from socialhome.services.notification_service import NotificationService
from socialhome.services.user_service import UserService


class _GuardianBlocks:
    """Child-protection stand-in: guardian blocks between pairs of users."""

    def __init__(self, *pairs: tuple[str, str]) -> None:
        self._pairs: dict[str, set[str]] = {}
        for a, b in pairs:
            self._pairs.setdefault(a, set()).add(b)
            self._pairs.setdefault(b, set()).add(a)

    async def guardian_block_counterparts(self, user_id: str) -> frozenset[str]:
        return frozenset(self._pairs.get(user_id, ()))

    async def is_guardian_blocked(self, a: str, b: str) -> bool:
        return b in self._pairs.get(a, ())

    async def is_protected(self, user_id: str) -> bool:
        return False

    async def is_restricted(self, user_id, capability) -> bool:
        return False

    def register_gate(self, gate) -> None:
        pass


@pytest.fixture
async def stack(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "audience.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    user_repo = SqliteUserRepo(db)
    space_repo = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x0c" * 32))
    notif_repo = SqliteNotificationRepo(db, max_per_user=50)
    notif_svc = NotificationService(notif_repo, user_repo, space_repo, bus)
    notif_svc.wire()

    class S:
        pass

    s = S()
    s.db = db
    s.bus = bus
    s.users = user_repo
    s.notif_repo = notif_repo
    s.notif_svc = notif_svc
    s.user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)
    yield s
    await db.shutdown()


async def _user(s, username: str, *, protected: bool = False):
    u = await s.user_svc.provision(username=username, display_name=username.title())
    if protected:
        await s.db.enqueue(
            "UPDATE users SET child_protection_enabled=1 WHERE user_id=?",
            (u.user_id,),
        )
        u = await s.users.get_by_user_id(u.user_id)
    return u


async def _remote(s, user_id: str, name: str, instance: str = "peer-r") -> None:
    await s.db.enqueue(
        "INSERT OR IGNORE INTO remote_instances(id, display_name,"
        " remote_identity_pk, key_self_to_remote, key_remote_to_self,"
        " remote_inbox_url, local_inbox_id, status, source) VALUES(?, ?, ?,"
        " 'k1', 'k2', 'https://peer/wh', ?, 'confirmed', 'manual')",
        (instance, instance, "00" * 32, f"wh-{instance}"),
    )
    await s.users.upsert_remote(
        RemoteUser(
            user_id=user_id,
            instance_id=instance,
            remote_username=name.lower(),
            display_name=name,
        )
    )


async def _space(s, sid: str, name: str, members: dict[str, str]) -> str:
    """A space *sid* named *name*; *members* maps user_id → role."""
    await s.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?, ?, 'inst', 'owner', ?)",
        (sid, name, "ab" * 32),
    )
    for uid, role in members.items():
        await s.db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
            (sid, uid, role),
        )
    return sid


async def _types(s, user) -> list[str]:
    return [n.type for n in await s.notif_repo.list(user.user_id, limit=50)]


async def _titles(s, user, ntype: str) -> list[str]:
    return [
        n.title
        for n in await s.notif_repo.list(user.user_id, limit=50)
        if n.type == ntype
    ]


def _task(**kw) -> Task:
    now = datetime.now(UTC)
    base = {
        "id": "t1",
        "list_id": "l1",
        "title": "Bins",
        "status": TaskStatus.TODO,
        "position": 0,
        "created_by": "creator",
        "created_at": now,
        "updated_at": now,
    }
    base.update(kw)
    return Task(**base)


# ── 1. Invite answers go to the inviter only, by name ─────────────────


async def test_invite_accepted_goes_to_the_inviter_only_and_names_who_and_where(
    stack,
):
    """Before: every admin got "Your invite was accepted." — no person, no
    space. Now only the inviter hears it, and it says who and where."""
    inviter = await _user(stack, "anna")
    other_admin = await _user(stack, "otto")
    sid = await _space(
        stack,
        "sp-inv",
        "Garden",
        {inviter.user_id: "owner", other_admin.user_id: "admin"},
    )
    await stack.bus.publish(
        RemoteSpaceInviteAccepted(
            space_id=sid,
            instance_id="peer-r",
            invitee_user_id="u-zoe",
            inviter_user_id=inviter.user_id,
            invitee_display_name="Zoe",
        )
    )
    assert await _titles(stack, inviter, "space_remote_invite_accepted") == [
        "Zoe accepted your invite to Garden"
    ]
    assert await _types(stack, other_admin) == []


async def test_invite_declined_goes_to_the_inviter_only_and_names_who_and_where(
    stack,
):
    inviter = await _user(stack, "anna")
    other_admin = await _user(stack, "otto")
    sid = await _space(
        stack,
        "sp-dec",
        "Garden",
        {inviter.user_id: "admin", other_admin.user_id: "owner"},
    )
    await _remote(stack, "u-zoe", "Zoe")
    await stack.bus.publish(
        RemoteSpaceInviteDeclined(
            space_id=sid,
            instance_id="peer-r",
            invitee_user_id="u-zoe",
            inviter_user_id=inviter.user_id,
        )
    )
    assert await _titles(stack, inviter, "space_remote_invite_declined") == [
        "Zoe declined your invite to Garden"
    ]
    assert await _types(stack, other_admin) == []


async def test_invite_answer_to_an_inviter_who_left_the_space_rings_nobody(stack):
    inviter = await _user(stack, "anna")
    admin = await _user(stack, "otto")
    sid = await _space(stack, "sp-left", "Garden", {admin.user_id: "owner"})
    await stack.bus.publish(
        RemoteSpaceInviteAccepted(
            space_id=sid,
            instance_id="peer-r",
            invitee_user_id="u-zoe",
            inviter_user_id=inviter.user_id,
            invitee_display_name="Zoe",
        )
    )
    assert await _types(stack, inviter) == []
    assert await _types(stack, admin) == []


async def test_invite_answer_without_a_known_name_says_someone_never_the_id(stack):
    inviter = await _user(stack, "anna")
    sid = await _space(stack, "sp-anon", "Garden", {inviter.user_id: "owner"})
    await stack.bus.publish(
        RemoteSpaceInviteDeclined(
            space_id=sid,
            instance_id="peer-r",
            invitee_user_id="u-unknown-hash",
            inviter_user_id=inviter.user_id,
        )
    )
    assert await _titles(stack, inviter, "space_remote_invite_declined") == [
        "Someone declined your invite to Garden"
    ]


async def test_invite_answer_from_someone_the_inviter_blocked_is_silent(stack):
    inviter = await _user(stack, "anna")
    sid = await _space(stack, "sp-blk", "Garden", {inviter.user_id: "owner"})
    await _remote(stack, "u-zoe", "Zoe")
    await stack.users.block(inviter.user_id, "u-zoe")
    await stack.bus.publish(
        RemoteSpaceInviteAccepted(
            space_id=sid,
            instance_id="peer-r",
            invitee_user_id="u-zoe",
            inviter_user_id=inviter.user_id,
            invitee_display_name="Zoe",
        )
    )
    assert await _types(stack, inviter) == []


# ── 2. Remote people are named, never a raw user id ───────────────────


async def test_join_request_from_a_remote_person_names_them_not_their_user_id(
    stack,
):
    admin = await _user(stack, "anna")
    sid = await _space(stack, "sp-jr", "Garden", {admin.user_id: "owner"})
    await _remote(stack, "u-zoe-hash", "Zoe")
    await stack.bus.publish(
        SpaceJoinRequested(space_id=sid, user_id="u-zoe-hash", request_id="r1")
    )
    assert await _titles(stack, admin, "space_join_requested") == [
        "Zoe wants to join Garden"
    ]


async def test_join_request_from_an_unknown_remote_says_someone_not_the_id(stack):
    admin = await _user(stack, "anna")
    sid = await _space(stack, "sp-jr2", "Garden", {admin.user_id: "owner"})
    await stack.bus.publish(
        SpaceJoinRequested(space_id=sid, user_id="u-unknown-hash", request_id="r1")
    )
    titles = await _titles(stack, admin, "space_join_requested")
    assert titles == ["Someone wants to join Garden"]
    assert "u-unknown-hash" not in titles[0]


async def test_member_joined_from_a_remote_person_names_them_not_their_user_id(
    stack,
):
    anna = await _user(stack, "anna")
    sid = await _space(stack, "sp-mj", "Garden", {anna.user_id: "owner"})
    await _remote(stack, "u-zoe-hash", "Zoe")
    await stack.bus.publish(
        SpaceMemberJoined(space_id=sid, user_id="u-zoe-hash", role="member")
    )
    assert await _titles(stack, anna, "space_member_joined") == ["Zoe joined Garden"]


# ── 3. Recipients are always local users ──────────────────────────────


async def test_task_deadline_for_a_remote_assignee_skips_them_and_still_tells_the_rest(
    stack,
):
    """Before: the remote assignee's row failed the users FK, the bus
    swallowed it, and every assignee after it in the loop heard nothing."""
    bob = await _user(stack, "bob")
    task = _task(assignees=("u-remote-assignee", bob.user_id))
    await stack.bus.publish(TaskDeadlineDue(task=task, due_date=date.today()))
    assert await _types(stack, bob) == ["task_deadline"]
    assert await stack.notif_repo.list("u-remote-assignee", limit=10) == []


async def test_post_moderated_for_a_remote_author_creates_no_row(stack):
    post = Post(
        id="p1",
        author="u-remote-author",
        type=PostType.TEXT,
        content="x",
        created_at=datetime.now(UTC),
    )
    await stack.notif_svc.on_space_post_moderated(
        SpacePostModerated(space_id="sp", post=post, moderated_by="mod")
    )
    assert await stack.notif_repo.list("u-remote-author", limit=10) == []


async def test_a_direct_save_for_an_unknown_user_id_is_refused(stack):
    """The gate lives in ``_save_notif`` itself, so a new type inherits it."""
    from socialhome.repositories.notification_repo import new_notification

    saved = await stack.notif_svc._save_notif(
        new_notification(user_id="u-nobody", type="x", title="t", link_url=None),
        actor_user_id=None,
    )
    assert saved is None
    assert await stack.notif_repo.list("u-nobody", limit=10) == []


# ── 4. Blocks and guardian blocks on every type ───────────────────────


async def _blocked_and_guarded(stack):
    """``bob`` blocked ``mallory``; ``kid``'s guardian blocked ``mallory``;
    ``carl`` blocked nobody. Returns ``(mallory, bob, kid, carl)``."""
    mallory = await _user(stack, "mallory")
    bob = await _user(stack, "bob")
    kid = await _user(stack, "kid")
    carl = await _user(stack, "carl")
    await stack.users.block(bob.user_id, mallory.user_id)
    stack.notif_svc.attach_child_protection(
        _GuardianBlocks((kid.user_id, mallory.user_id))
    )
    return mallory, bob, kid, carl


async def test_task_assigned_by_someone_blocked_or_guardian_blocked_is_silent(stack):
    mallory, bob, kid, carl = await _blocked_and_guarded(stack)
    for who in (bob, kid, carl):
        await stack.bus.publish(
            TaskAssigned(
                task=_task(id=f"t-{who.username}", created_by=mallory.user_id),
                assigned_to=who.user_id,
                assigned_by=mallory.user_id,
            )
        )
    assert await _types(stack, bob) == []
    assert await _types(stack, kid) == []
    assert await _types(stack, carl) == ["task_assigned"]


async def test_task_assigned_by_someone_else_than_the_creator_checks_the_assigner(
    stack,
):
    """An update by mallory assigns bob to carl's task: bob blocked mallory,
    so no bell — the creator is not who acted."""
    mallory, bob, _kid, carl = await _blocked_and_guarded(stack)
    await stack.bus.publish(
        TaskAssigned(
            task=_task(created_by=carl.user_id),
            assigned_to=bob.user_id,
            assigned_by=mallory.user_id,
        )
    )
    assert await _types(stack, bob) == []


async def test_task_completed_by_someone_blocked_or_guardian_blocked_is_silent(stack):
    mallory, bob, kid, carl = await _blocked_and_guarded(stack)
    await stack.bus.publish(
        TaskCompleted(
            task=_task(assignees=(bob.user_id, kid.user_id, carl.user_id)),
            completed_by=mallory.user_id,
        )
    )
    assert await _types(stack, bob) == []
    assert await _types(stack, kid) == []
    assert await _types(stack, carl) == ["task_completed"]


async def test_space_calendar_event_by_someone_blocked_or_guardian_blocked_is_silent(
    stack,
):
    mallory, bob, kid, carl = await _blocked_and_guarded(stack)
    sid = await _space(
        stack,
        "sp-cal",
        "Garden",
        {
            mallory.user_id: "owner",
            bob.user_id: "member",
            kid.user_id: "member",
            carl.user_id: "member",
        },
    )
    now = datetime.now(UTC)
    await stack.bus.publish(
        CalendarEventCreated(
            event=CalendarEvent(
                id="e1",
                calendar_id=sid,
                summary="Party",
                start=now,
                end=now,
                created_by=mallory.user_id,
            )
        )
    )
    assert await _types(stack, bob) == []
    assert await _types(stack, kid) == []
    assert await _types(stack, carl) == ["calendar_event_created"]


async def test_member_joined_and_join_request_by_someone_blocked_are_silent(stack):
    mallory, bob, kid, carl = await _blocked_and_guarded(stack)
    sid = await _space(
        stack,
        "sp-j",
        "Garden",
        {bob.user_id: "owner", kid.user_id: "admin", carl.user_id: "admin"},
    )
    await stack.bus.publish(
        SpaceJoinRequested(space_id=sid, user_id=mallory.user_id, request_id="r1")
    )
    await stack.bus.publish(
        SpaceMemberJoined(space_id=sid, user_id=mallory.user_id, role="member")
    )
    assert await _types(stack, bob) == []
    assert await _types(stack, kid) == []
    assert sorted(await _types(stack, carl)) == [
        "space_join_requested",
        "space_member_joined",
    ]


async def test_bazaar_bells_about_someone_blocked_or_guardian_blocked_are_silent(
    stack,
):
    mallory, bob, kid, carl = await _blocked_and_guarded(stack)
    for who in (bob, kid, carl):
        # mallory bids on their listing …
        await stack.bus.publish(
            BazaarBidPlaced(
                listing_post_id=f"l-{who.username}",
                seller_user_id=who.user_id,
                bidder_user_id=mallory.user_id,
                amount=5,
                new_end_time="2099-01-01T00:00:00+00:00",
            )
        )
        # … and accepts / rejects their offer on hers.
        await stack.bus.publish(
            BazaarOfferAccepted(
                listing_post_id="l-m",
                seller_user_id=mallory.user_id,
                buyer_user_id=who.user_id,
                price=5,
            )
        )
        await stack.bus.publish(
            BazaarOfferRejected(
                listing_post_id="l-m",
                seller_user_id=mallory.user_id,
                bidder_user_id=who.user_id,
                bid_id="b",
            )
        )
    assert await _types(stack, bob) == []
    assert await _types(stack, kid) == []
    assert sorted(await _types(stack, carl)) == [
        "bazaar_bid_placed",
        "bazaar_offer_accepted",
        "bazaar_offer_rejected",
    ]


async def test_app_challenge_from_someone_guardian_blocked_is_silent(stack):
    mallory, bob, kid, carl = await _blocked_and_guarded(stack)
    for who in (bob, kid, carl):
        await stack.bus.publish(
            AppChallengeReceived(
                app_id="chess",
                session_id=f"s-{who.username}",
                to_user_id=who.user_id,
                from_display="Mallory",
                from_user_id=mallory.user_id,
            )
        )
    assert await _types(stack, bob) == []
    assert await _types(stack, kid) == []
    assert await _types(stack, carl) == ["app_challenge"]


async def test_every_save_notif_call_names_its_actor():
    """New types inherit the block gate only if they say who acted: every
    ``_save_notif`` call in the service passes ``actor_user_id`` (``None``
    for a notice nobody caused, e.g. a deadline or an expiry)."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(ns_module)))
    # The RSVP cohort's update / cancel bells are being reworked in their own
    # change (each household tells its own cohort, the actor excluded); they
    # join this rule there.
    pending = {"on_calendar_event_deleted", "on_calendar_event_updated"}
    missing = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef) or fn.name in pending:
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_save_notif"
                and not any(k.arg == "actor_user_id" for k in node.keywords)
            ):
                missing.append(f"{fn.name}:{node.lineno}")
    assert missing == []


# ── 5. Child protection ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("hop_count", "received_via", "notified"),
    [
        (1, None, True),
        (1, "household", True),
        (2, "household", False),
        (1, "gfs", False),
    ],
    ids=["local", "paired", "relayed", "gfs"],
)
async def test_a_protected_author_hears_of_reactions_from_its_circle_only(
    stack, hop_count, received_via, notified
):
    """§CP.R: a protected account sees moments from its household and paired
    households only — a reaction from further away must not reach it as a
    notification naming a stranger (the same gate replies have)."""
    kid = await _user(stack, "kid", protected=True)
    await _remote(stack, "u-far", "Far")
    await stack.bus.publish(
        MomentReactionChanged(
            moment_id="m1",
            reactor_user_id="u-far",
            author_user_id=kid.user_id,
            emoji="👍",
            received_via=received_via,
            hop_count=hop_count,
        )
    )
    assert ("moment_reacted" in await _types(stack, kid)) is notified


async def test_an_unprotected_author_hears_of_relayed_reactions(stack):
    anna = await _user(stack, "anna")
    await stack.bus.publish(
        MomentReactionChanged(
            moment_id="m1",
            reactor_user_id="u-far",
            author_user_id=anna.user_id,
            emoji="👍",
            received_via="gfs",
            hop_count=2,
        )
    )
    assert await _types(stack, anna) == ["moment_reacted"]


async def test_location_enabled_never_reaches_followers_or_protected_accounts(stack):
    actor = await _user(stack, "anna")
    member = await _user(stack, "bob")
    follower = await _user(stack, "fred")
    kid = await _user(stack, "kid", protected=True)
    sid = await _space(
        stack,
        "sp-loc",
        "Garden",
        {
            actor.user_id: "owner",
            member.user_id: "member",
            follower.user_id: "subscriber",
            kid.user_id: "member",
        },
    )
    await stack.bus.publish(
        SpaceLocationFeatureEnabled(
            space_id=sid, space_name="Garden", actor_user_id=actor.user_id
        )
    )
    assert await _types(stack, member) == ["space_location_enabled"]
    assert await _types(stack, follower) == []
    assert await _types(stack, kid) == []
    assert await _types(stack, actor) == []
