"""Requests that need you stay on top (D12): the "Needs you" rows.

A join request, an invite and a pairing request are each one row
(``join_request:<id>`` …) that stays pending — read or not — until someone
handles it; then every recipient's row says how it ended and who ended it
("Handled by Søren"), reads as read and leaves every device's section. A
space's review queue stays one row per space. :meth:`pending_actions`
checks each row against the request it asks about.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.apps import AppManifest, AppPendingSession, InstalledApp
from socialhome.domain.events import (
    AppChallengeEnded,
    AppChallengeReceived,
    AutoPairRequestAnswered,
    AutoPairRequestIncoming,
    NotificationReadChanged,
    RemoteJoinRequestApproved,
    RemoteJoinRequestDenied,
    RemoteSpaceInviteReceived,
    SpaceJoinApproved,
    SpaceJoinDenied,
    SpaceJoinRequested,
    SpaceModerationApproved,
)
from socialhome.domain.federation import PairingStatus
from socialhome.domain.notification import NotificationObject, object_key
from socialhome.domain.space import (
    JoinMode,
    JoinRequestHandledError,
    ModerationStatus,
    SpaceModerationItem,
    SpacePermissionError,
    SpaceRole,
    SpaceType,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.app_repo import SqliteAppRepo
from socialhome.repositories.notification_repo import (
    SqliteNotificationRepo,
    new_notification,
)
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.notification_service import NotificationService
from socialhome.services.space_service import SpaceService
from socialhome.services.user_service import UserService


class _Peers:
    """Paired households, as the federation repo answers them."""

    def __init__(self) -> None:
        self.confirmed: set[str] = set()

    async def get_instance(self, instance_id: str):
        status = (
            PairingStatus.CONFIRMED
            if instance_id in self.confirmed
            else PairingStatus.PENDING_SENT
        )
        return SimpleNamespace(id=instance_id, status=status, proto_version=99)


class _Inbox:
    def __init__(self) -> None:
        self.waiting: set[str] = set()

    def get(self, request_id: str):
        return object() if request_id in self.waiting else None


class _Push:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def push_to_user(self, user_id, payload) -> None:
        self.sent.append((user_id, payload.title))


@pytest.fixture
async def env(tmp_dir):
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
    users = SqliteUserRepo(db)
    spaces = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x0c" * 32))
    notifs = SqliteNotificationRepo(db, max_per_user=50)
    space_svc = SpaceService(
        spaces, SqliteSpacePostRepo(db), users, bus, own_instance_id=iid
    )
    svc = NotificationService(notifs, users, spaces, bus)
    peers, inbox, push = _Peers(), _Inbox(), _Push()
    svc.attach_federation_repo(peers)
    svc.attach_pair_inbox(inbox)
    svc.attach_push_service(push)
    svc.wire()
    user_svc = UserService(users, bus, own_instance_public_key=kp.public_key)

    e = SimpleNamespace(
        db=db,
        bus=bus,
        spaces=spaces,
        notifs=notifs,
        space_svc=space_svc,
        svc=svc,
        peers=peers,
        inbox=inbox,
        push=push,
        read_changed=[],
    )

    async def _rc(ev: NotificationReadChanged) -> None:
        e.read_changed.append(ev)

    bus.subscribe(NotificationReadChanged, _rc)

    async def user(name: str, **kw):
        return await user_svc.provision(username=name, display_name=name, **kw)

    e.user = user
    yield e
    await db.shutdown()


async def _salon(env, owner: str = "anna", **kw):
    return await env.space_svc.create_space(
        owner_username=owner,
        name="Salon",
        join_mode=JoinMode.REQUEST,
        space_type=SpaceType.PUBLIC,
        **kw,
    )


async def _rows(env, user_id: str, type: str):
    return [n for n in await env.notifs.list(user_id, limit=50) if n.type == type]


# ── Join requests: one row per request ────────────────────────────────


async def test_each_join_request_is_its_own_pinned_row(env):
    anna, ben = await env.user("anna"), await env.user("ben")
    kim, lena = await env.user("kim"), await env.user("lena")
    space = await _salon(env)
    await env.space_svc.add_member(
        space.id, actor_username="anna", user_id=ben.user_id, role=SpaceRole.ADMIN
    )
    r1 = await env.space_svc.request_join(space.id, user_id=kim.user_id)
    r2 = await env.space_svc.request_join(space.id, user_id=lena.user_id)

    rows = await _rows(env, anna.user_id, "space_join_requested")
    assert {n.object_key for n in rows} == {
        object_key(NotificationObject.JOIN_REQUEST, r1),
        object_key(NotificationObject.JOIN_REQUEST, r2),
    }
    assert all(n.space_id == space.id and n.params.flags == () for n in rows)
    # Reading one never un-pins it.
    await env.notifs.mark_all_read(anna.user_id)
    assert len(await env.svc.pending_actions(anna.user_id)) == 2


async def test_one_admin_handles_it_and_it_leaves_every_admins_section(env):
    anna, ben = await env.user("anna"), await env.user("ben")
    kim, lena = await env.user("kim"), await env.user("lena")
    space = await _salon(env)
    await env.space_svc.add_member(
        space.id, actor_username="anna", user_id=ben.user_id, role=SpaceRole.ADMIN
    )
    r1 = await env.space_svc.request_join(space.id, user_id=kim.user_id)
    r2 = await env.space_svc.request_join(space.id, user_id=lena.user_id)
    env.read_changed.clear()

    await env.space_svc.approve_join_request(r1, actor_username="ben")

    key = object_key(NotificationObject.JOIN_REQUEST, r1)
    for admin in (anna, ben):
        (row,) = [
            n
            for n in await _rows(env, admin.user_id, "space_join_requested")
            if n.object_key == key
        ]
        assert row.is_read
        assert row.params.handled.outcome == "approved"
        assert row.params.handled.by.name == "ben"
        pending = await env.svc.pending_actions(admin.user_id)
        assert [n.object_key for n in pending] == [
            object_key(NotificationObject.JOIN_REQUEST, r2)
        ]
    # Every admin's devices hear which item left the section.
    assert {(e.user_id, e.handled) for e in env.read_changed} == {
        (anna.user_id, (key,)),
        (ben.user_id, (key,)),
    }

    await env.space_svc.deny_join_request(r2, actor_username="anna")
    (row,) = [
        n
        for n in await _rows(env, ben.user_id, "space_join_requested")
        if n.object_key == object_key(NotificationObject.JOIN_REQUEST, r2)
    ]
    assert row.params.handled.outcome == "declined"
    assert row.params.handled.by.name == "anna"
    assert await env.svc.pending_actions(ben.user_id) == []


async def test_the_admin_who_lost_the_race_hears_who_won(env):
    await env.user("anna")
    ben, kim = await env.user("ben"), await env.user("kim")
    space = await _salon(env)
    await env.space_svc.add_member(
        space.id, actor_username="anna", user_id=ben.user_id, role=SpaceRole.ADMIN
    )
    rid = await env.space_svc.request_join(space.id, user_id=kim.user_id)
    await env.space_svc.approve_join_request(rid, actor_username="anna")
    with pytest.raises(JoinRequestHandledError) as exc:
        await env.space_svc.deny_join_request(rid, actor_username="ben")
    assert exc.value.status == 409
    assert exc.value.params == {"status": "approved", "by": "anna"}
    with pytest.raises(JoinRequestHandledError):
        await env.space_svc.approve_join_request(rid, actor_username="ben")
    # The decline never went through.
    assert await env.spaces.get_member(space.id, kim.user_id) is not None


class _Federation:
    """Records what the host sends back to a remote applicant's household."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send_with_mesh_fallback(self, *, to_instance_id, event_type, payload):
        # Yield like a real network send so the racing admin runs meanwhile.
        await asyncio.sleep(0)
        self.sent.append((event_type.value, payload))


async def _two_admins_and_a_remote_request(env):
    await env.user("anna")
    ben = await env.user("ben")
    space = await _salon(env)
    await env.space_svc.add_member(
        space.id, actor_username="anna", user_id=ben.user_id, role=SpaceRole.ADMIN
    )
    fed = _Federation()
    env.space_svc.attach_federation(fed, env.peers, None)
    rid = await env.spaces.save_join_request(
        space.id, "u-remote", remote_applicant_instance_id="peer-remote"
    )
    answered: list = []

    async def _seen(ev) -> None:
        answered.append(ev)

    env.bus.subscribe(SpaceJoinApproved, _seen)
    env.bus.subscribe(SpaceJoinDenied, _seen)
    return space, rid, fed, answered


async def _tokens(env, space_id: str) -> int:
    return int(
        await env.db.fetchval(
            "SELECT COUNT(*) FROM space_invite_tokens WHERE space_id=?", (space_id,)
        )
    )


@pytest.mark.parametrize(
    "second", ["approve_join_request", "deny_join_request"], ids=["approve", "deny"]
)
async def test_two_admins_answering_at_once_only_one_answer_goes_out(env, second):
    """Both admins click at the same moment. Exactly one answer wins: one
    federation reply, at most one invite token, one domain event — the
    other admin hears who answered, and nothing of theirs goes out."""
    space, rid, fed, answered = await _two_admins_and_a_remote_request(env)
    results = await asyncio.gather(
        env.space_svc.approve_join_request(rid, actor_username="anna"),
        getattr(env.space_svc, second)(rid, actor_username="ben"),
        return_exceptions=True,
    )
    lost = [r for r in results if isinstance(r, BaseException)]
    assert len(lost) == 1, results
    assert isinstance(lost[0], JoinRequestHandledError)
    assert len(fed.sent) == 1
    assert len(answered) == 1
    row = await env.spaces.get_join_request(rid)
    winner = "approved" if isinstance(answered[0], SpaceJoinApproved) else "denied"
    assert row["status"] == winner
    assert lost[0].params["status"] == winner
    assert await _tokens(env, space.id) == (1 if winner == "approved" else 0)


async def test_two_local_approvals_at_once_seat_once(env):
    await env.user("anna")
    ben, kim = await env.user("ben"), await env.user("kim")
    space = await _salon(env)
    await env.space_svc.add_member(
        space.id, actor_username="anna", user_id=ben.user_id, role=SpaceRole.ADMIN
    )
    rid = await env.space_svc.request_join(space.id, user_id=kim.user_id)
    approved: list = []

    async def _seen(ev) -> None:
        approved.append(ev)

    env.bus.subscribe(SpaceJoinApproved, _seen)
    results = await asyncio.gather(
        env.space_svc.approve_join_request(rid, actor_username="anna"),
        env.space_svc.approve_join_request(rid, actor_username="ben"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, JoinRequestHandledError) for r in results) == 1
    assert len(approved) == 1
    assert await env.spaces.get_member(space.id, kim.user_id) is not None


async def test_only_an_admin_of_that_space_may_decline_a_request(env):
    """A request id used to be enough to decline it — and to federate the
    refusal — for any signed-in person."""
    await env.user("anna")
    kim, mo = await env.user("kim"), await env.user("mo")
    space = await _salon(env)
    other = await env.space_svc.create_space(
        owner_username="anna", name="Other", space_type=SpaceType.PRIVATE
    )
    rid = await env.space_svc.request_join(space.id, user_id=kim.user_id)
    with pytest.raises(SpacePermissionError):
        await env.space_svc.deny_join_request(rid, actor_username="mo")
    with pytest.raises(SpacePermissionError):
        await env.space_svc.deny_join_request(rid, actor_username="kim")
    # Named through another space's URL: unknown there.
    with pytest.raises(KeyError):
        await env.space_svc.deny_join_request(
            rid, actor_username="anna", space_id=other.id
        )
    with pytest.raises(KeyError):
        await env.space_svc.approve_join_request(
            rid, actor_username="anna", space_id=other.id
        )
    assert (await env.spaces.get_join_request(rid))["status"] == "pending"
    assert mo.user_id  # used


async def test_unusual_requests_are_flagged_for_review(env):
    """Near the edge, an unpaired household, an admin grant, an age limit:
    never one tap from the list."""
    anna = await env.user("anna")
    space = await _salon(env)
    env.peers.confirmed.add("paired-house")
    edge = await env.spaces.save_join_request(
        space.id,
        "remote-kim",
        remote_applicant_instance_id="stranger-house",
        area_verdict="edge",
    )
    paired = await env.spaces.save_join_request(
        space.id, "remote-zoe", remote_applicant_instance_id="paired-house"
    )
    grant = await env.spaces.save_join_request(
        space.id, anna.user_id, requested_role="admin"
    )
    for rid, uid in ((edge, "remote-kim"), (paired, "remote-zoe")):
        await env.bus.publish(
            SpaceJoinRequested(space_id=space.id, user_id=uid, request_id=rid)
        )
    by_key = {
        n.object_key: n.params.flags
        for n in await _rows(env, anna.user_id, "space_join_requested")
    }
    assert by_key == {
        object_key(NotificationObject.JOIN_REQUEST, edge): ("edge", "unpaired"),
        object_key(NotificationObject.JOIN_REQUEST, paired): (),
    }
    assert await env.svc._join_request_flags(space, grant) == ("admin",)
    aged = SimpleNamespace(min_age=16)
    assert await env.svc._join_request_flags(aged, paired) == ("age",)
    assert await env.svc._join_request_flags(space, "unknown") == ("unpaired",)


async def test_pending_actions_drop_work_that_is_gone(env):
    """A request answered or expired behind the service's back, a seat that
    lost its admin role, an emptied queue: each resolves on the next look."""
    anna, ben = await env.user("anna"), await env.user("ben")
    kim, lena = await env.user("kim"), await env.user("lena")
    space = await _salon(env)
    await env.space_svc.add_member(
        space.id, actor_username="anna", user_id=ben.user_id, role=SpaceRole.ADMIN
    )
    r1 = await env.space_svc.request_join(space.id, user_id=kim.user_id)
    await env.space_svc.request_join(space.id, user_id=lena.user_id)
    await env.spaces.update_join_request_status(r1, "expired")
    # A pre-per-request row keyed by the space, and a review-queue row.
    for type in ("space_join_requested", "moderation_pending"):
        legacy = new_notification(user_id=anna.user_id, type=type, title="t")
        await env.svc._save_notif(
            legacy,
            about=SimpleNamespace(
                object_key=object_key(NotificationObject.SPACE, space.id),
                space_id=space.id,
                space_name="Salon",
            ),
        )

    pending = await env.svc.pending_actions(anna.user_id)
    assert sorted(n.type for n in pending) == [
        "space_join_requested",  # Lena's request
        "space_join_requested",  # the space-wide row: Lena still waits
    ]
    gone = [
        n
        for n in await env.notifs.list(anna.user_id, limit=50)
        if n.params and n.params.handled
    ]
    assert {(n.type, n.params.handled.outcome) for n in gone} == {
        ("space_join_requested", "done"),  # Kim's expired request
        ("moderation_pending", "done"),  # nothing to review
    }

    # Ben steps down: nothing waits for him any more.
    await env.space_svc.set_role(
        space.id, actor_username="anna", user_id=ben.user_id, role="member"
    )
    assert await env.svc.pending_actions(ben.user_id) == []


async def test_the_review_queue_row_says_who_emptied_it(env):
    anna = await env.user("anna")
    space = await _salon(env)
    await env.svc.on_moderation_queued(
        SimpleNamespace(item=SimpleNamespace(space_id=space.id))
    )
    item = SpaceModerationItem(
        id="i1",
        space_id=space.id,
        feature="posts",
        action="create",
        submitted_by="x",
        payload={},
        current_snapshot=None,
        submitted_at=None,
        expires_at=None,
        status=ModerationStatus.APPROVED,
        reviewed_by=anna.user_id,
    )
    await env.bus.publish(SpaceModerationApproved(item=item))
    (row,) = await _rows(env, anna.user_id, "moderation_pending")
    assert row.params.handled.outcome == "done"
    assert row.params.handled.by.name == "anna"


# ── Invites ───────────────────────────────────────────────────────────


async def test_a_local_invite_is_a_row_that_opens_the_invite(env):
    await env.user("anna")
    bob = await env.user("bob")
    space = await env.space_svc.create_space(
        owner_username="anna", name="Garden", space_type=SpaceType.PRIVATE
    )
    inv = await env.space_svc.invite_local_user(
        space.id, actor_username="anna", user_id=bob.user_id
    )
    (row,) = await _rows(env, bob.user_id, "space_invite")
    assert row.title == "anna invited you to Garden"
    # The invite card, where a local community asks for location consent —
    # never a one-tap join.
    assert row.link_url == "/spaces"
    assert row.object_key == object_key(NotificationObject.SPACE_INVITE, space.id)
    assert row.params.actors[0].name == "anna"
    # This household's own space: the push may name it.
    assert (bob.user_id, "anna invited you to Garden") in env.push.sent
    assert [n.id for n in await env.svc.pending_actions(bob.user_id)] == [row.id]

    await env.space_svc.decline_local_invite(inv, user_id=bob.user_id)
    (row,) = await _rows(env, bob.user_id, "space_invite")
    assert row.params.handled.outcome == "declined"
    assert row.params.handled.by is None
    assert await env.svc.pending_actions(bob.user_id) == []


async def test_accepting_a_local_invite_resolves_it(env):
    await env.user("anna")
    bob = await env.user("bob")
    space = await env.space_svc.create_space(
        owner_username="anna", name="Garden", space_type=SpaceType.PRIVATE
    )
    inv = await env.space_svc.invite_local_user(
        space.id, actor_username="anna", user_id=bob.user_id
    )
    await env.space_svc.accept_local_invite(inv, user_id=bob.user_id)
    (row,) = await _rows(env, bob.user_id, "space_invite")
    assert row.params.handled.outcome == "accepted"


async def test_an_unpaired_households_invite_never_puts_the_space_on_the_lock_screen(
    env,
):
    """D11: the row names the space in the app; the push only when the
    inviter's household is paired with this one."""
    bob = await env.user("bob")
    env.peers.confirmed.add("paired-house")
    await env.bus.publish(
        RemoteSpaceInviteReceived(
            space_id="sp-far",
            inviter_user_id="zoe-id",
            invitee_user_id=bob.user_id,
            inviter_instance_id="stranger-house",
            space_name="Nachbarschaft",
        )
    )
    await env.bus.publish(
        RemoteSpaceInviteReceived(
            space_id="sp-near",
            inviter_user_id="zoe-id",
            invitee_user_id=bob.user_id,
            inviter_instance_id="paired-house",
            space_name="Salon",
        )
    )
    titles = {n.title for n in await _rows(env, bob.user_id, "space_invite")}
    assert titles == {
        "Someone invited you to Nachbarschaft",
        "Someone invited you to Salon",
    }
    pushed = [t for uid, t in env.push.sent if uid == bob.user_id]
    assert pushed == [
        "Someone invited you to a space",
        "Someone invited you to Salon",
    ]
    # A relayed invite names no household at all: no space either.
    await env.bus.publish(
        RemoteSpaceInviteReceived(
            space_id="sp-x",
            inviter_user_id="zoe-id",
            invitee_user_id=bob.user_id,
        )
    )
    assert env.push.sent[-1] == (bob.user_id, "Someone invited you to a space")
    # An invite the invitee has no row for (gone): not pending.
    assert await env.svc.pending_actions(bob.user_id) == []


async def test_an_invite_for_someone_not_here_is_dropped(env):
    await env.bus.publish(
        RemoteSpaceInviteReceived(
            space_id="sp", inviter_user_id="z", invitee_user_id="nobody"
        )
    )
    assert env.push.sent == []


# ── Pairing requests ──────────────────────────────────────────────────


async def test_a_pairing_request_goes_to_every_admin_and_resolves_for_all(env):
    admin = await env.user("anna", is_admin=True)
    other = await env.user("ole", is_admin=True)
    kid = await env.user("kid")
    env.inbox.waiting.add("pr1")
    await env.bus.publish(
        AutoPairRequestIncoming(
            request_id="pr1",
            from_a_id="gamma",
            via_b_id="beta",
            from_a_display="Gamma House",
            via_b_display="Beta House",
        )
    )
    (row,) = await _rows(env, admin.user_id, "pairing_request")
    assert row.title == "Gamma House wants to pair with your household"
    assert row.link_url == "/connections"
    assert await _rows(env, kid.user_id, "pairing_request") == []
    assert [n.id for n in await env.svc.pending_actions(admin.user_id)] == [row.id]

    env.inbox.waiting.clear()
    await env.bus.publish(
        AutoPairRequestAnswered(
            request_id="pr1", approved=True, by_user_id=other.user_id
        )
    )
    for uid in (admin.user_id, other.user_id):
        (row,) = await _rows(env, uid, "pairing_request")
        assert row.params.handled.outcome == "approved"
        assert row.params.handled.by.name == "ole"


async def test_a_pairing_request_the_inbox_forgot_is_not_pending(env):
    admin = await env.user("anna", is_admin=True)
    await env.bus.publish(
        AutoPairRequestIncoming(
            request_id="pr2",
            from_a_id="gamma",
            via_b_id="beta",
            from_a_display="Gamma House",
            via_b_display="Beta House",
        )
    )
    # Expired (the inbox lives in memory and forgets after its TTL).
    assert await env.svc.pending_actions(admin.user_id) == []
    (row,) = await _rows(env, admin.user_id, "pairing_request")
    assert row.params.handled.outcome == "done"


async def test_without_an_inbox_a_pairing_request_stays_for_admins_only(env):
    admin = await env.user("anna", is_admin=True)
    env.svc.attach_pair_inbox(None)
    note = new_notification(user_id=admin.user_id, type="pairing_request", title="t")
    about = SimpleNamespace(
        object_key=object_key(NotificationObject.PAIRING_REQUEST, "x"),
        space_id=None,
        space_name=None,
    )
    await env.svc._save_notif(note, about=about)
    assert len(await env.svc.pending_actions(admin.user_id)) == 1
    await env.db.enqueue(
        "UPDATE users SET is_admin=0 WHERE user_id=?", (admin.user_id,)
    )
    assert await env.svc.pending_actions(admin.user_id) == []


# ── The applicant hears another household's answer ────────────────────


async def test_the_applicant_hears_a_remote_hosts_answer(env):
    """§D2: our member asked to join a space another household hosts; its
    answer reaches the bell from the events this household already gets."""
    kim = await env.user("kim")
    approved = await env.spaces.save_join_request(
        "sp-far", kim.user_id, remote_applicant_instance_id="host-house"
    )
    denied = await env.spaces.save_join_request(
        "sp-other", kim.user_id, remote_applicant_instance_id="host-house"
    )
    await env.bus.publish(
        RemoteJoinRequestApproved(
            request_id=approved, space_id="sp-far", invite_token="tok"
        )
    )
    await env.bus.publish(RemoteJoinRequestDenied(request_id=denied, space_id="x"))
    (yes,) = await _rows(env, kim.user_id, "space_join_approved")
    (no,) = await _rows(env, kim.user_id, "space_join_denied")
    assert yes.title == "Your join request was approved"
    assert yes.link_url == "/spaces/sp-far"
    assert no.title == "Your join request was declined."
    # The request's own space, not the one the answer claims.
    assert no.space_id == "sp-other"


async def test_a_remote_answer_names_a_space_this_household_holds(env):
    await env.user("anna")
    kim = await env.user("kim")
    space = await _salon(env)
    rid = await env.spaces.save_join_request(
        space.id, kim.user_id, remote_applicant_instance_id="host-house"
    )
    await env.bus.publish(
        RemoteJoinRequestApproved(request_id=rid, space_id=space.id, invite_token="t")
    )
    (yes,) = await _rows(env, kim.user_id, "space_join_approved")
    assert yes.title == "Your request to join Salon was approved"


async def test_a_remote_answer_for_no_request_of_ours_is_ignored(env):
    kim = await env.user("kim")
    local = await env.spaces.save_join_request("sp", kim.user_id)
    for rid in ("unknown", local):
        await env.bus.publish(RemoteJoinRequestDenied(request_id=rid, space_id="sp"))
    remote_user = await env.spaces.save_join_request(
        "sp", "remote-id", remote_applicant_instance_id="h"
    )
    await env.bus.publish(
        RemoteJoinRequestDenied(request_id=remote_user, space_id="sp")
    )
    assert await env.notifs.list(kim.user_id, limit=10) == []


# ── A request to message you, from before direct chats needed none ──


async def test_an_old_request_to_message_you_resolves_and_keeps_its_title(env):
    """1:1s between paired households need no request, so nothing makes a
    ``dm_contact_requested`` row any more. One kept from an older build
    still reads from its stored title and leaves "Needs you" on the next
    look — never pinned forever."""
    anna = await env.user("anna")
    old = new_notification(
        user_id=anna.user_id,
        type="dm_contact_requested",
        title="Ben would like to message you",
        link_url="/dms",
    )
    await env.svc._save_notif(
        old,
        about=SimpleNamespace(
            object_key="contact:ru-ben", space_id=None, space_name=None
        ),
    )
    (row,) = await _rows(env, anna.user_id, "dm_contact_requested")
    assert row.is_pending_action
    assert await env.svc.pending_actions(anna.user_id) == []
    (row,) = await _rows(env, anna.user_id, "dm_contact_requested")
    assert row.params.handled.outcome == "done"
    assert row.is_read
    assert row.title == "Ben would like to message you"


# ── Game invites ──────────────────────────────────────────────────────


async def _invite_env(env, *, created_at: str | None = None):
    anna = await env.user("anna")
    apps = SqliteAppRepo(env.db)
    env.svc.attach_app_repo(apps)
    await apps.install(
        InstalledApp(
            app_id="chess",
            name="Chess",
            version="1",
            enabled=True,
            manifest=AppManifest(entry="index.html", icon=None, capabilities=()),
            bundle_path="apps/chess/1",
            bundle_sha256="ab" * 32,
            source_url="https://example/chess.tgz",
            installed_by=None,
            installed_at="2026-06-02T00:00:00+00:00",
        )
    )
    for sid in ("s1", "s2"):
        await apps.add_pending_session(
            AppPendingSession(
                app_id="chess",
                user_id=anna.user_id,
                session_id=sid,
                from_instance="peer-b",
                from_user="ben",
                payload={"verb": "open"},
                created_at=created_at or datetime.now(UTC).isoformat(),
            )
        )
        await env.bus.publish(
            AppChallengeReceived(
                app_id="chess",
                session_id=sid,
                to_user_id=anna.user_id,
                from_display="Ben",
            )
        )
    return anna, apps


def _invite_key(sid: str) -> str:
    return object_key(NotificationObject.APP_SESSION, f"chess/{sid}")


async def test_each_game_invite_is_its_own_pinned_row(env):
    anna, _ = await _invite_env(env)
    rows = await _rows(env, anna.user_id, "app_challenge")
    assert {n.object_key for n in rows} == {_invite_key("s1"), _invite_key("s2")}
    assert all(n.link_url == "/apps/chess" for n in rows)
    await env.notifs.mark_all_read(anna.user_id)
    assert len(await env.svc.pending_actions(anna.user_id)) == 2


async def test_a_game_invite_ends_as_it_ended(env):
    anna, _ = await _invite_env(env)
    await env.bus.publish(
        AppChallengeEnded(
            app_id="chess",
            user_id=anna.user_id,
            session_ids=("s1",),
            outcome="declined",
        )
    )
    await env.bus.publish(
        AppChallengeEnded(
            app_id="chess",
            user_id=anna.user_id,
            session_ids=("s2",),
            outcome="withdrawn",
        )
    )
    outcomes = {
        n.object_key: n.params.handled.outcome
        for n in await _rows(env, anna.user_id, "app_challenge")
    }
    assert outcomes == {_invite_key("s1"): "declined", _invite_key("s2"): "withdrawn"}


async def test_an_expired_game_invite_reads_as_expired(env):
    old = (datetime.now(UTC) - timedelta(days=15)).isoformat()
    anna, _ = await _invite_env(env, created_at=old)
    assert await env.svc.pending_actions(anna.user_id) == []
    assert {
        n.params.handled.outcome
        for n in await _rows(env, anna.user_id, "app_challenge")
    } == {"expired"}


async def test_a_game_invite_that_is_gone_or_whose_app_is_off_is_done(env):
    anna, apps = await _invite_env(env)
    await apps.take_pending_session("chess", anna.user_id, "s1")
    (left,) = await env.svc.pending_actions(anna.user_id)
    assert left.object_key == _invite_key("s2")
    await apps.set_enabled("chess", enabled=False)
    assert await env.svc.pending_actions(anna.user_id) == []
    assert {
        n.params.handled.outcome
        for n in await _rows(env, anna.user_id, "app_challenge")
    } == {"done"}


async def test_a_game_invite_row_from_before_one_row_per_invite_is_done(env):
    anna = await env.user("anna")
    legacy = new_notification(user_id=anna.user_id, type="app_challenge", title="t")
    await env.svc._save_notif(
        legacy,
        about=SimpleNamespace(
            object_key=object_key(NotificationObject.APP, "chess"),
            space_id=None,
            space_name=None,
        ),
    )
    assert await env.svc.pending_actions(anna.user_id) == []
