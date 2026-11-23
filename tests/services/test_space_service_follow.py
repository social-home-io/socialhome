"""Following a space is joining it with the read-only role (v_96).

A follower is a member with the ``subscriber`` role on every path: a follow
of a space this household hosts seats on the members' path; a follow of a
space hosted elsewhere is a ``SPACE_JOIN_REQUEST`` with ``requested_role =
subscriber`` the host approves on its own; unfollowing is the members'
self-leave without the immediate rotation.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.errors import NotFoundError
from socialhome.domain.events import (
    SpaceJoinApproved,
    SpaceJoinRequested,
    SpaceMemberJoined,
    SpaceMemberLeft,
)
from socialhome.domain.federation import (
    DeliveryResult,
    FederationEventType,
    InstanceSource,
    PairingStatus,
)
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.public_space import PublicSpaceListing
from socialhome.domain.space import (
    HostTooOldError,
    JoinMode,
    SpaceFeatures,
    SpaceRole,
    SpaceType,
    SubscribeNotAllowedError,
)
from socialhome.federation.invite_token_redeem import (
    SpaceInviteTokenRedeemCoordinator,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.public_space_repo import SqlitePublicSpaceRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_service import SpaceService, stub_space_from_metadata
from socialhome.services.user_service import UserService

PEER = "peer-house"
HOST = "host-house"


@pytest.fixture
async def stack(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "follow.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        """INSERT INTO instance_identity(instance_id, identity_private_key,
           identity_public_key, routing_secret) VALUES(?,?,?,?)""",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    user_repo = SqliteUserRepo(db)
    space_repo = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x09" * 32))
    svc = SpaceService(
        space_repo, SqliteSpacePostRepo(db), user_repo, bus, own_instance_id=iid
    )
    fed = AsyncMock()
    fed.send_with_mesh_fallback = AsyncMock(
        return_value=DeliveryResult(instance_id=PEER, ok=True)
    )
    fed.send_event = AsyncMock(return_value=DeliveryResult(instance_id=HOST, ok=True))
    fed.broadcast_to_space_members = AsyncMock()
    fed.peer_supports = AsyncMock(return_value=True)
    fed.space_member_supports = AsyncMock(return_value=True)
    fed.own_identity_pk = b"\x11" * 32
    fed.own_instance_id = iid
    fed_repo = MagicMock()
    fed_repo.get_instance = AsyncMock(
        return_value=SimpleNamespace(
            status=PairingStatus.CONFIRMED, source=InstanceSource.MANUAL
        )
    )
    remote_members = SqliteSpaceRemoteMemberRepo(db)
    svc.attach_federation(
        federation_service=fed,
        federation_repo=fed_repo,
        remote_member_repo=remote_members,
    )
    events: list = []

    async def _record(event):
        events.append(event)

    for kind in (SpaceJoinRequested, SpaceJoinApproved, SpaceMemberJoined):
        bus.subscribe(kind, _record)
    bus.subscribe(SpaceMemberLeft, _record)
    user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)

    async def provision(username):
        return await user_svc.provision(username=username, display_name=username)

    yield SimpleNamespace(
        db=db,
        bus=bus,
        svc=svc,
        spaces=space_repo,
        users=user_repo,
        fed=fed,
        fed_repo=fed_repo,
        remote=remote_members,
        iid=iid,
        events=events,
        provision=provision,
    )
    await db.shutdown()


async def _hosted(stack, *, allow_subscribers=True, join_mode=JoinMode.INVITE_ONLY):
    await stack.provision("owner")
    return await stack.svc.create_space(
        owner_username="owner",
        name="Open",
        space_type=SpaceType.GLOBAL,
        join_mode=join_mode,
        features=SpaceFeatures(allow_subscribers=allow_subscribers),
    )


async def _token_row(stack, space_id: str) -> dict:
    row = await stack.db.fetchone(
        "SELECT * FROM space_invite_tokens WHERE space_id=?", (space_id,)
    )
    assert row is not None
    return dict(row)


def _approved_sends(stack) -> list[dict]:
    return [
        c.kwargs
        for c in stack.fed.send_with_mesh_fallback.await_args_list
        if c.kwargs["event_type"] is FederationEventType.SPACE_JOIN_REQUEST_APPROVED
    ]


async def _approve_follow(stack, space_id, *, user_id="u-fan", request_id="rq-1"):
    return await stack.svc.approve_remote_follow(
        space_id=space_id,
        instance_id=PEER,
        user_id=user_id,
        request_id=request_id,
    )


# ── Host: automatic approval ──────────────────────────────────────────────


async def test_a_remote_follow_is_approved_with_a_bound_subscriber_token(stack):
    space = await _hosted(stack)  # invite-only: a follow is not a join
    stack.events.clear()

    assert await _approve_follow(stack, space.id) is True

    row = await stack.spaces.get_join_request("rq-1")
    assert row is not None and row["status"] == "approved"
    assert row["remote_applicant_instance_id"] == PEER
    token = await _token_row(stack, space.id)
    assert token["role"] == SpaceRole.SUBSCRIBER.value
    assert token["bound_instance_id"] == PEER
    assert token["bound_user_id"] == "u-fan"
    assert token["uses_remaining"] == 1
    (sent,) = _approved_sends(stack)
    assert sent["to_instance_id"] == PEER
    assert sent["payload"]["invite_token"] == token["token"]
    assert sent["payload"]["request_id"] == "rq-1"
    # An automatic approval names no reviewer.
    assert "reviewed_by" not in sent["payload"]
    # No admin bell, no "approved by" — a follow needs nobody.
    assert not [
        e for e in stack.events if isinstance(e, SpaceJoinRequested | SpaceJoinApproved)
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"features": SpaceFeatures(allow_subscribers=False)},
        {"dissolved": True},
        {"archived": True},
        {"space_type": SpaceType.PRIVATE},
    ],
)
async def test_a_remote_follow_fails_closed(stack, change):
    space = await _hosted(stack)
    await stack.spaces.save(replace(space, **change))

    assert await _approve_follow(stack, space.id) is False
    assert await stack.spaces.get_join_request("rq-1") is None
    assert _approved_sends(stack) == []


async def test_a_follow_of_a_space_hosted_elsewhere_is_not_approved_here(stack):
    stub = await _remote_stub(stack)
    assert await _approve_follow(stack, stub.id) is False
    assert _approved_sends(stack) == []


async def test_a_banned_person_cannot_follow(stack):
    space = await _hosted(stack)
    await stack.spaces.ban_member(space.id, "u-fan", banned_by="owner", reason="x")
    assert await _approve_follow(stack, space.id) is False
    assert _approved_sends(stack) == []


async def test_a_reused_request_id_is_refused(stack):
    space = await _hosted(stack)
    assert await _approve_follow(stack, space.id) is True
    assert await _approve_follow(stack, space.id, user_id="u-other") is False
    assert len(_approved_sends(stack)) == 1


async def test_the_host_drops_a_follow_from_a_seated_household_user(stack):
    """F3 (c), host side: a member who follows keeps their member seat."""
    space = await _hosted(stack)
    await stack.remote.add(
        space_id=space.id,
        instance_id=PEER,
        user_id="u-fan",
        user_pk=None,
        display_name="Fan",
        role=SpaceRole.MEMBER.value,
    )
    assert await _approve_follow(stack, space.id) is False
    seat = await stack.remote.get(space.id, PEER, "u-fan")
    assert seat is not None and seat.role == SpaceRole.MEMBER.value
    assert _approved_sends(stack) == []


async def test_an_admins_approval_still_seats_a_member(stack):
    """The shared helper keeps the admin path as it was: a member token,
    the reviewer named, SpaceJoinApproved."""
    space = await _hosted(stack, join_mode=JoinMode.REQUEST)
    await stack.spaces.save_join_request(
        space.id,
        "u-fan",
        remote_applicant_instance_id=PEER,
        request_id="rq-2",
    )
    owner = await stack.users.get("owner")
    await stack.svc.approve_join_request("rq-2", actor_username="owner")
    token = await _token_row(stack, space.id)
    assert token["role"] == SpaceRole.MEMBER.value
    (sent,) = _approved_sends(stack)
    assert sent["payload"]["reviewed_by"] == owner.user_id
    assert any(isinstance(e, SpaceJoinApproved) for e in stack.events)


# ── Applicant: the request ────────────────────────────────────────────────


def _join_request_sends(stack) -> list[dict]:
    return [
        c.kwargs
        for c in stack.fed.send_event.await_args_list
        if c.kwargs["event_type"] is FederationEventType.SPACE_JOIN_REQUEST
    ]


async def test_a_follow_request_carries_the_requested_role(stack):
    fan = await stack.provision("fan")
    await stack.svc.request_join_remote(
        "sp-remote",
        applicant_user_id=fan.user_id,
        host_instance_id=HOST,
        requested_role=SpaceRole.SUBSCRIBER,
    )
    (sent,) = _join_request_sends(stack)
    assert sent["to_instance_id"] == HOST
    assert sent["payload"]["requested_role"] == "subscriber"
    stack.fed.peer_supports.assert_any_await(
        HOST, min_version=FederationCapability.MIN_FOR_SUBSCRIBER_SEATS
    )


async def test_a_plain_join_request_carries_no_role(stack):
    fan = await stack.provision("fan")
    await stack.svc.request_join_remote(
        "sp-remote", applicant_user_id=fan.user_id, host_instance_id=HOST
    )
    (sent,) = _join_request_sends(stack)
    assert "requested_role" not in sent["payload"]


async def test_a_follow_of_a_host_below_v96_is_refused(stack):
    fan = await stack.provision("fan")
    stack.fed.peer_supports = AsyncMock(return_value=False)
    with pytest.raises(HostTooOldError) as exc:
        await stack.svc.request_join_remote(
            "sp-remote",
            applicant_user_id=fan.user_id,
            host_instance_id=HOST,
            requested_role=SpaceRole.SUBSCRIBER,
        )
    assert exc.value.feature == "follow"
    assert _join_request_sends(stack) == []
    assert await stack.spaces.list_pending_join_requests("sp-remote") == []


async def test_a_follow_over_the_gfs_passes_the_role_through(stack):
    fan = await stack.provision("fan")
    stack.fed_repo.get_instance = AsyncMock(return_value=None)
    gfs_join = MagicMock()
    gfs_join.host_of_listing = AsyncMock(return_value=HOST)
    gfs_join.request_join = AsyncMock(return_value="rq-gfs")
    stack.svc._gfs_join = gfs_join
    rid = await stack.svc.request_join_remote(
        "sp-remote",
        applicant_user_id=fan.user_id,
        host_instance_id=HOST,
        requested_role=SpaceRole.SUBSCRIBER,
    )
    assert rid == "rq-gfs"
    assert (
        gfs_join.request_join.await_args.kwargs["requested_role"]
        is SpaceRole.SUBSCRIBER
    )


# ── follow_space ──────────────────────────────────────────────────────────


async def _remote_stub(stack, space_id="sp-remote", *, allow_subscribers=True):
    stub = stub_space_from_metadata(
        space_id,
        host_instance_id=HOST,
        meta={
            "name": "Remote",
            "identity_public_key": "ab" * 32,
            "space_type": "global",
            "join_mode": "invite_only",
            "features": {"allow_subscribers": allow_subscribers},
            "owner_username": "",
        },
    )
    await stack.spaces.save(stub)
    return stub


async def test_following_a_hosted_space_seats_a_subscriber_on_the_members_path(
    stack,
):
    space = await _hosted(stack)
    fan = await stack.provision("fan")
    with patch.object(
        SpaceService, "_emit_member_roster_gossip", AsyncMock()
    ) as gossip:
        got = await stack.svc.follow_space(fan.user_id, space.id)
    assert got == {"subscribed": True, "pending": False}
    seat = await stack.spaces.get_member(space.id, fan.user_id)
    assert seat is not None and seat.role == SpaceRole.SUBSCRIBER
    # The v_23 roster gossip a member's seat gets.
    gossip.assert_awaited_once()
    assert gossip.await_args.kwargs["role"] == SpaceRole.SUBSCRIBER
    assert gossip.await_args.kwargs["tombstoned"] is False


async def test_following_a_hosted_space_without_followers_is_refused(stack):
    space = await _hosted(stack, allow_subscribers=False)
    fan = await stack.provision("fan")
    with pytest.raises(SubscribeNotAllowedError):
        await stack.svc.follow_space(fan.user_id, space.id)


async def test_following_a_space_hosted_elsewhere_sends_a_follow_request(stack):
    await _remote_stub(stack)
    fan = await stack.provision("fan")
    got = await stack.svc.follow_space(fan.user_id, "sp-remote")
    assert got == {"subscribed": False, "pending": True}
    (sent,) = _join_request_sends(stack)
    assert sent["to_instance_id"] == HOST
    assert sent["payload"]["requested_role"] == "subscriber"
    assert "join_location" not in sent["payload"]
    # Nothing seated here until the host's approval is redeemed.
    assert await stack.spaces.get_member("sp-remote", fan.user_id) is None

    # Asking again while it waits sends nothing more.
    again = await stack.svc.follow_space(fan.user_id, "sp-remote")
    assert again == {"subscribed": False, "pending": True}
    assert len(_join_request_sends(stack)) == 1


async def test_following_a_stub_without_followers_is_refused(stack):
    await _remote_stub(stack, allow_subscribers=False)
    fan = await stack.provision("fan")
    with pytest.raises(SubscribeNotAllowedError):
        await stack.svc.follow_space(fan.user_id, "sp-remote")
    assert _join_request_sends(stack) == []


async def test_following_a_listed_space_asks_the_listings_host(stack):
    fan = await stack.provision("fan")
    listings = SqlitePublicSpaceRepo(stack.db)
    await listings.upsert(
        PublicSpaceListing(
            space_id="sp-listed",
            instance_id=HOST,
            name="Listed",
            allow_subscribers=True,
        )
    )
    stack.svc.attach_public_space_directory(listings)
    got = await stack.svc.follow_space(fan.user_id, "sp-listed")
    assert got == {"subscribed": False, "pending": True}
    (sent,) = _join_request_sends(stack)
    assert sent["to_instance_id"] == HOST


async def test_following_a_listing_without_followers_is_refused(stack):
    fan = await stack.provision("fan")
    listings = SqlitePublicSpaceRepo(stack.db)
    await listings.upsert(
        PublicSpaceListing(space_id="sp-listed", instance_id=HOST, name="Listed")
    )
    stack.svc.attach_public_space_directory(listings)
    with pytest.raises(SubscribeNotAllowedError):
        await stack.svc.follow_space(fan.user_id, "sp-listed")


async def test_following_a_space_only_a_gfs_lists_asks_through_the_gfs(stack):
    fan = await stack.provision("fan")
    stack.fed_repo.get_instance = AsyncMock(return_value=None)
    gfs_join = MagicMock()
    gfs_join.host_of_listing = AsyncMock(return_value=HOST)
    gfs_join.request_join = AsyncMock(return_value="rq-gfs")
    stack.svc._gfs_join = gfs_join
    got = await stack.svc.follow_space(fan.user_id, "sp-gfs")
    assert got == {"subscribed": False, "pending": True}
    assert gfs_join.request_join.await_args.kwargs["host_instance_id"] == HOST


async def test_following_an_unknown_space_is_not_found(stack):
    fan = await stack.provision("fan")
    with pytest.raises(NotFoundError):
        await stack.svc.follow_space(fan.user_id, "sp-nowhere")


async def test_a_seated_member_who_follows_keeps_their_seat(stack):
    """F3 (c), member side: no demotion, nothing sent."""
    space = await _hosted(stack)
    owner = await stack.users.get("owner")
    got = await stack.svc.follow_space(owner.user_id, space.id)
    assert got == {"subscribed": True, "pending": False}
    seat = await stack.spaces.get_member(space.id, owner.user_id)
    assert seat is not None and seat.role == SpaceRole.OWNER

    stub = await _remote_stub(stack)
    fan = await stack.provision("fan")
    await stack.svc._seat_local_member(
        stub, fan.user_id, role=SpaceRole.MEMBER, actor_user_id=fan.user_id
    )
    got = await stack.svc.follow_space(fan.user_id, stub.id)
    assert got == {"subscribed": True, "pending": False}
    seat = await stack.spaces.get_member(stub.id, fan.user_id)
    assert seat is not None and seat.role == SpaceRole.MEMBER
    assert _join_request_sends(stack) == []


# ── Unfollow = leave, without the immediate rotation ─────────────────────


async def test_unfollowing_leaves_without_rotating(stack):
    space = await _hosted(stack)
    fan = await stack.provision("fan")
    await stack.svc.follow_space(fan.user_id, space.id)
    with (
        patch.object(
            SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
        ) as rotate,
        patch.object(SpaceService, "_emit_member_roster_gossip", AsyncMock()) as gossip,
    ):
        await stack.svc.unfollow_space(fan.user_id, space.id)
    assert await stack.spaces.get_member(space.id, fan.user_id) is None
    rotate.assert_not_awaited()
    assert gossip.await_args.kwargs["tombstoned"] is True
    assert any(
        isinstance(e, SpaceMemberLeft) and e.role == SpaceRole.SUBSCRIBER
        for e in stack.events
    )


async def test_unfollowing_never_ends_a_members_seat(stack):
    space = await _hosted(stack)
    owner = await stack.users.get("owner")
    await stack.svc.unfollow_space(owner.user_id, space.id)
    assert await stack.spaces.get_member(space.id, owner.user_id) is not None


async def test_unfollowing_a_space_hosted_elsewhere_tells_the_host(stack):
    stub = await _remote_stub(stack)
    fan = await stack.provision("fan")
    await stack.svc._seat_local_member(
        stub, fan.user_id, role=SpaceRole.SUBSCRIBER, actor_user_id=fan.user_id
    )
    with (
        patch.object(
            SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
        ) as rotate,
        patch.object(SpaceService, "_notify_host_of_leave", AsyncMock()) as notify,
    ):
        await stack.svc.unfollow_space(fan.user_id, stub.id)
    notify.assert_awaited_once()
    assert notify.await_args.args[1] == fan.user_id
    rotate.assert_not_awaited()


async def test_a_followers_leave_space_does_not_rotate_but_a_members_does(stack):
    space = await _hosted(stack)
    fan = await stack.provision("fan")
    mem = await stack.provision("mem")
    await stack.svc.follow_space(fan.user_id, space.id)
    await stack.svc._seat_local_member(
        space, mem.user_id, role=SpaceRole.MEMBER, actor_user_id=mem.user_id
    )
    with patch.object(
        SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
    ) as rotate:
        await stack.svc.remove_member(
            space.id, actor_username="fan", user_id=fan.user_id
        )
        rotate.assert_not_awaited()
        await stack.svc.remove_member(
            space.id, actor_username="mem", user_id=mem.user_id
        )
        rotate.assert_awaited_once_with(space.id)


# ── Host: a remote household's leave ─────────────────────────────────────


async def _seat_remote(stack, space_id, user_id, role):
    await stack.remote.add(
        space_id=space_id,
        instance_id=PEER,
        user_id=user_id,
        user_pk=None,
        display_name=user_id,
        role=role.value,
    )
    await stack.spaces.add_space_instance(space_id, PEER)


async def test_a_remote_followers_leave_does_not_rotate(stack):
    space = await _hosted(stack)
    await _seat_remote(stack, space.id, "u-fan", SpaceRole.SUBSCRIBER)
    with patch.object(
        SpaceService, "rotate_if_writer_scope_weakened", AsyncMock()
    ) as rotate:
        assert await stack.svc.on_remote_member_left(space.id, PEER, "u-fan")
    rotate.assert_not_awaited()
    assert await stack.remote.get(space.id, PEER, "u-fan") is None
    assert PEER not in await stack.spaces.list_member_instances(space.id)


async def test_a_mixed_member_and_follower_leave_rotates(stack):
    space = await _hosted(stack)
    await _seat_remote(stack, space.id, "u-fan", SpaceRole.SUBSCRIBER)
    await _seat_remote(stack, space.id, "u-mem", SpaceRole.MEMBER)
    with patch.object(
        SpaceService, "rotate_if_writer_scope_weakened", AsyncMock()
    ) as rotate:
        assert await stack.svc.on_remote_member_left(space.id, PEER, None)
    rotate.assert_awaited_once()


# ── F3: leave as a member, come back as a follower ───────────────────────


def _redeemer(stack) -> SpaceInviteTokenRedeemCoordinator:
    return SpaceInviteTokenRedeemCoordinator(
        bus=stack.bus,
        federation_service=SimpleNamespace(own_instance_id=stack.iid),  # type: ignore[arg-type]
        space_repo=stack.spaces,
        space_remote_member_repo=stack.remote,
        user_repo=stack.users,
        federation_repo=stack.fed_repo,
    )


async def test_f3a_a_remote_member_leaves_then_follows_as_a_subscriber(stack):
    """The leave rotates; the follow is approved for the tombstoned seat,
    redeemed into a ``subscriber`` seat, and rotates nothing more."""
    space = await _hosted(stack)
    await _seat_remote(stack, space.id, "u-fan", SpaceRole.MEMBER)
    with (
        patch.object(
            SpaceService, "rotate_if_writer_scope_weakened", AsyncMock()
        ) as rotate,
        patch.object(
            SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
        ) as full_rotate,
    ):
        assert await stack.svc.on_remote_member_left(space.id, PEER, "u-fan")
        assert rotate.await_count == 1

        assert await _approve_follow(stack, space.id) is True
        token = (await _token_row(stack, space.id))["token"]
        ack, reason, reservation = await _redeemer(stack)._consume_seat_and_build_ack(
            token=token,
            redeemer_instance_id=PEER,
            redeemer_user_id="u-fan",
            redeemer_pk=None,
            redeemer_display="Fan",
        )
        assert reason is None and ack is not None and reservation is not None
        assert ack["role"] == SpaceRole.SUBSCRIBER.value
        seat = await stack.remote.get(space.id, PEER, "u-fan")
        assert seat is not None and seat.role == SpaceRole.SUBSCRIBER.value
        assert rotate.await_count == 1
        full_rotate.assert_not_awaited()


async def test_f3b_a_local_member_leaves_then_follows_as_a_subscriber(stack):
    space = await _hosted(stack)
    mem = await stack.provision("mem")
    await stack.svc._seat_local_member(
        space, mem.user_id, role=SpaceRole.MEMBER, actor_user_id=mem.user_id
    )
    with patch.object(
        SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
    ) as rotate:
        await stack.svc.remove_member(
            space.id, actor_username="mem", user_id=mem.user_id
        )
        assert rotate.await_count == 1
        got = await stack.svc.follow_space(mem.user_id, space.id)
        assert got == {"subscribed": True, "pending": False}
        assert rotate.await_count == 1
    seat = await stack.spaces.get_member(space.id, mem.user_id)
    assert seat is not None and seat.role == SpaceRole.SUBSCRIBER
