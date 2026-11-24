"""Following a space is joining it with the read-only role (v_96).

A follower is a member with the ``subscriber`` role on every path: a follow
of a space this household hosts seats on the members' path; a follow of a
space hosted elsewhere is a ``SPACE_JOIN_REQUEST`` with ``requested_role =
subscriber`` the host approves on its own; unfollowing is the members'
self-leave without the immediate rotation.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from socialhome.crypto import (
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
)
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
from socialhome.domain.local_community import Eligibility
from socialhome.domain.public_space import PublicSpaceListing
from socialhome.domain.space import (
    AgeRestrictedError,
    HostTooOldError,
    JoinMode,
    SpaceFeatures,
    SpaceMember,
    SpaceRole,
    SpaceType,
    SubscribeNotAllowedError,
)
from socialhome.federation.gfs_join_request import APPROVAL_TOKEN_TTL
from socialhome.federation.invite_token_redeem import (
    SpaceInviteTokenRedeemCoordinator,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.peer_space_directory_repo import (
    PeerSpaceDirectoryEntry,
    SqlitePeerSpaceDirectoryRepo,
)
from socialhome.repositories.public_space_repo import SqlitePublicSpaceRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_service import SpaceService, stub_space_from_metadata
from socialhome.services.space_writer_cert_service import SpaceWriterCertService
from socialhome.services.user_service import UserService

PEER = "peer-house"
HOST = "host-house"

#: The applicant household's identity key (pinned on its peer row): its
#: people's user ids derive from it (P5 person-ID anchor).
PEER_IDENTITY = generate_identity_keypair()
PEER_PK = PEER_IDENTITY.public_key
U_FAN = derive_user_id(PEER_PK, "fan")
U_OTHER = derive_user_id(PEER_PK, "other")
_ANCHORS = {U_FAN: "fan", U_OTHER: "other"}


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
        own_pk=kp.public_key,
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


def _proof(user_id: str = U_FAN) -> dict:
    """The applicant household's proof of ``user_id`` (P5)."""
    return {"applicant_identity_pk": PEER_PK, "applicant_anchor": _ANCHORS.get(user_id)}


async def _approve_follow(stack, space_id, *, user_id=U_FAN, request_id="rq-1"):
    return await stack.svc.approve_remote_follow(
        space_id=space_id,
        instance_id=PEER,
        user_id=user_id,
        request_id=request_id,
        **_proof(user_id),
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
    assert token["bound_user_id"] == U_FAN
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


async def test_a_follow_through_the_gfs_is_answered_through_the_gfs(stack):
    """A follow that came over the GFS (v_84 relay contact kept with the
    request) is answered the same way: a 48 h bound subscriber token sealed
    back through the GFS, never a direct send."""
    space = await _hosted(stack)
    gfs_join = MagicMock()
    gfs_join.send_answer = AsyncMock(return_value=True)
    stack.svc._gfs_join = gfs_join
    relay = '{"side": "host", "gfs_url": "https://gfs.example"}'

    assert (
        await stack.svc.approve_remote_follow(
            space_id=space.id,
            instance_id=PEER,
            user_id=U_FAN,
            request_id="rq-g",
            gfs_relay=relay,
            **_proof(),
        )
        is True
    )

    token = await _token_row(stack, space.id)
    assert token["role"] == SpaceRole.SUBSCRIBER.value
    assert token["bound_instance_id"] == PEER
    assert token["bound_user_id"] == U_FAN
    row = gfs_join.send_answer.await_args.args[0]
    assert row["id"] == "rq-g" and row["gfs_relay"] == relay
    kwargs = gfs_join.send_answer.await_args.kwargs
    assert kwargs["answer"] == "approved"
    assert kwargs["invite_token"] == token["token"]
    assert kwargs["token_expires_at"] == token["expires_at"]
    expires = datetime.fromisoformat(token["expires_at"])
    assert expires - datetime.now(UTC) > APPROVAL_TOKEN_TTL - timedelta(minutes=1)
    assert _approved_sends(stack) == []


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
    await stack.spaces.ban_member(space.id, U_FAN, banned_by="owner", reason="x")
    assert await _approve_follow(stack, space.id) is False
    assert _approved_sends(stack) == []


async def test_a_follow_without_the_seat_store_fails_closed(stack):
    """Without the remote seat store "not seated already" cannot be proved:
    the follow is refused, never approved blind."""
    space = await _hosted(stack)
    stack.svc._remote_members = None
    assert await _approve_follow(stack, space.id) is False
    assert _approved_sends(stack) == []


async def test_a_follow_naming_a_local_user_is_refused(stack, caplog):
    """The applicant picks ``user_id``: one of THIS household's people is
    never seated as another household's follower — their id derives from
    OUR key, so the applicant cannot prove it (P5)."""
    space = await _hosted(stack)
    local = await stack.provision("local")
    with caplog.at_level("INFO"):
        got = await stack.svc.approve_remote_follow(
            space_id=space.id,
            instance_id=PEER,
            user_id=local.user_id,
            request_id="rq-1",
            applicant_identity_pk=PEER_PK,
            applicant_anchor="local",
        )
    assert got is False
    assert _approved_sends(stack) == []
    assert await stack.spaces.get_join_request("rq-1") is None


@pytest.mark.parametrize(
    "case", ["somebody-elses-id", "wrong-anchor", "missing-anchor", "no-key"]
)
async def test_a_follow_is_approved_only_for_a_proven_person(stack, case):
    """P5 person-ID anchor: ``derive_user_id(applicant household key,
    anchor)`` must be the ``user_id`` — else refused, nothing stored."""
    space = await _hosted(stack)
    victim = derive_user_id(generate_identity_keypair().public_key, "fan")
    kwargs = {
        "somebody-elses-id": {"user_id": victim, **_proof()},
        "wrong-anchor": {
            "user_id": U_FAN,
            "applicant_identity_pk": PEER_PK,
            "applicant_anchor": "eve",
        },
        "missing-anchor": {"user_id": U_FAN, "applicant_identity_pk": PEER_PK},
        "no-key": {"user_id": U_FAN, "applicant_anchor": "fan"},
    }[case]
    got = await stack.svc.approve_remote_follow(
        space_id=space.id, instance_id=PEER, request_id="rq-1", **kwargs
    )
    assert got is False
    assert await stack.spaces.get_join_request("rq-1") is None
    assert _approved_sends(stack) == []


@pytest.mark.parametrize("tombstoned", [False, True])
async def test_a_legacy_squat_no_longer_locks_the_real_person_out(stack, tombstoned):
    """P4's first-come rule refused an id seated (now or before) under
    another household — so a squatter that got there first locked the real
    person out for good. With the proof, the real person's own household
    is approved whatever a legacy squat seat says."""
    space = await _hosted(stack)
    await stack.remote.add(
        space_id=space.id,
        instance_id="other-house",
        user_id=U_FAN,
        user_pk=None,
        display_name="Fan",
        role=SpaceRole.MEMBER.value,
    )
    if tombstoned:
        await stack.remote.remove(space.id, "other-house", U_FAN)
    assert await _approve_follow(stack, space.id) is True
    assert len(_approved_sends(stack)) == 1


@pytest.mark.parametrize("state", ["unknown", "seated", "tombstoned", "banned"])
async def test_an_unproven_follow_is_refused_before_any_roster_read(stack, state):
    """No probing signal: an unproven follow is refused before the ban,
    seat or roster is read, so the outcome is the same whatever they hold."""
    space = await _hosted(stack)
    victim = derive_user_id(generate_identity_keypair().public_key, "fan")
    if state in ("seated", "tombstoned"):
        await stack.remote.add(
            space_id=space.id,
            instance_id=PEER,
            user_id=victim,
            user_pk=None,
            display_name="V",
            role=SpaceRole.MEMBER.value,
        )
        if state == "tombstoned":
            await stack.remote.remove(space.id, PEER, victim)
    elif state == "banned":
        await stack.spaces.ban_member(space.id, victim, banned_by="owner", reason="x")
    with (
        patch.object(SqliteSpaceRepo, "is_banned", AsyncMock()) as banned,
        patch.object(SqliteSpaceRemoteMemberRepo, "get", AsyncMock()) as seat,
    ):
        got = await stack.svc.approve_remote_follow(
            space_id=space.id,
            instance_id=PEER,
            user_id=victim,
            request_id="rq-1",
            **_proof(),
        )
    assert got is False
    banned.assert_not_awaited()
    seat.assert_not_awaited()
    assert _approved_sends(stack) == []


async def _hosted_local_community(stack):
    space = await _hosted(stack)
    community = replace(
        space, space_type=SpaceType.PUBLIC, lat=47.37, lon=8.54, radius_km=5.0
    )
    await stack.spaces.save(community)
    return community


@pytest.mark.parametrize(
    ("verdict", "approved"),
    [(None, False), (Eligibility.EDGE, False), (Eligibility.INSIDE, True)],
)
async def test_a_local_community_follow_needs_the_inside_verdict(
    stack, verdict, approved
):
    """Following a local community is inside only: the approver itself
    refuses anything but an INSIDE verdict (a GFS follow passes none)."""
    space = await _hosted_local_community(stack)
    got = await stack.svc.approve_remote_follow(
        space_id=space.id,
        instance_id=PEER,
        user_id=U_FAN,
        request_id="rq-1",
        area_verdict=verdict,
        **_proof(),
    )
    assert got is approved
    assert len(_approved_sends(stack)) == (1 if approved else 0)


async def test_a_reused_request_id_is_refused(stack):
    space = await _hosted(stack)
    assert await _approve_follow(stack, space.id) is True
    assert await _approve_follow(stack, space.id, user_id=U_OTHER) is False
    assert len(_approved_sends(stack)) == 1


async def test_the_host_drops_a_follow_from_a_seated_household_user(stack):
    """F3 (c), host side: a member who follows keeps their member seat."""
    space = await _hosted(stack)
    await stack.remote.add(
        space_id=space.id,
        instance_id=PEER,
        user_id=U_FAN,
        user_pk=None,
        display_name="Fan",
        role=SpaceRole.MEMBER.value,
    )
    assert await _approve_follow(stack, space.id) is False
    seat = await stack.remote.get(space.id, PEER, U_FAN)
    assert seat is not None and seat.role == SpaceRole.MEMBER.value
    assert _approved_sends(stack) == []


async def test_an_admins_approval_still_seats_a_member(stack):
    """The shared helper keeps the admin path as it was: a member token,
    the reviewer named, SpaceJoinApproved."""
    space = await _hosted(stack, join_mode=JoinMode.REQUEST)
    await stack.spaces.save_join_request(
        space.id,
        U_FAN,
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
    # P5 — the person's anchor rides inside the payload: it proves their id
    # against our identity key.
    assert derive_user_id(stack.own_pk, sent["payload"]["user_anchor"]) == fan.user_id
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


async def test_a_paired_follow_waits_briefly_then_asks_again(stack):
    """The host answers a follow on its own, at once — or drops it without
    a word (ban, outside the area, followers off …). The applicant's row
    lives 15 minutes, so a dropped follow is not stuck "pending" for days:
    after that a new follow asks again."""
    await _remote_stub(stack)
    fan = await stack.provision("fan")
    await stack.svc.follow_space(fan.user_id, "sp-remote")
    (sent,) = _join_request_sends(stack)
    row = await stack.spaces.get_join_request(sent["payload"]["request_id"])
    assert row is not None
    expires = datetime.fromisoformat(row["expires_at"])
    assert timedelta(minutes=14) < expires - datetime.now(UTC) <= timedelta(minutes=15)

    await stack.db.enqueue(
        "UPDATE space_join_requests SET expires_at=? WHERE id=?",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), row["id"]),
    )
    again = await stack.svc.follow_space(fan.user_id, "sp-remote")
    assert again == {"subscribed": False, "pending": True}
    assert len(_join_request_sends(stack)) == 2


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


async def test_following_a_space_a_paired_household_lists_asks_that_host(stack):
    """A paired household's public spaces arrive in its directory
    (SPACE_DIRECTORY_SYNC), not in the GFS cache: a follow of one asks the
    household that lists it. The directory carries no ``allow_subscribers``
    — the host decides."""
    fan = await stack.provision("fan")
    peers = SqlitePeerSpaceDirectoryRepo(stack.db)
    await peers.replace_snapshot(
        HOST,
        [PeerSpaceDirectoryEntry(instance_id=HOST, space_id="sp-peer", name="P")],
    )
    stack.svc.attach_public_space_directory(
        SqlitePublicSpaceRepo(stack.db), peer_directory=peers
    )
    got = await stack.svc.follow_space(fan.user_id, "sp-peer")
    assert got == {"subscribed": False, "pending": True}
    (sent,) = _join_request_sends(stack)
    assert sent["to_instance_id"] == HOST
    assert sent["payload"]["requested_role"] == "subscriber"


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
    kwargs = gfs_join.request_join.await_args.kwargs
    assert kwargs["host_instance_id"] == HOST
    assert kwargs["requested_role"] is SpaceRole.SUBSCRIBER


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


async def test_turning_followers_off_drops_only_this_households_followers(stack):
    """Followers off: our own people's follower seats end (their own
    leave); a mirrored row of another household's person is never "left"
    in their name. The host rotates once for the switch itself."""
    space = await _hosted(stack)
    fan = await stack.provision("fan")
    await stack.svc.follow_space(fan.user_id, space.id)
    await stack.spaces.save_member(
        SpaceMember(
            space_id=space.id,
            user_id="u-mirror",
            role=SpaceRole.SUBSCRIBER.value,
            joined_at="2026-11-01T00:00:00+00:00",
        )
    )
    real_unfollow = SpaceService.unfollow_space
    with (
        patch.object(
            SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
        ) as rotate,
        patch.object(SpaceService, "unfollow_space", autospec=True) as unfollow,
    ):
        unfollow.side_effect = real_unfollow
        await stack.svc.update_config(
            space.id,
            actor_username="owner",
            features=replace(space.features, allow_subscribers=False),
        )
    assert [c.args[1] for c in unfollow.await_args_list] == [fan.user_id]
    rotate.assert_awaited_once_with(space.id)
    assert await stack.spaces.get_member(space.id, fan.user_id) is None
    assert await stack.spaces.get_member(space.id, "u-mirror") is not None


async def test_an_admin_removing_a_local_follower_does_not_rotate(stack):
    """A removal (not a ban) of a follower rotates nothing at once — the
    rotation is owed, like the follower's own leave (P5)."""
    space = await _hosted(stack)
    fan = await stack.provision("fan")
    await stack.svc.follow_space(fan.user_id, space.id)
    with patch.object(
        SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
    ) as rotate:
        await stack.svc.remove_member(
            space.id, actor_username="owner", user_id=fan.user_id
        )
    rotate.assert_not_awaited()
    assert await stack.spaces.get_member(space.id, fan.user_id) is None


async def test_an_admin_removing_a_local_member_still_rotates(stack):
    space = await _hosted(stack)
    mem = await stack.provision("mem")
    await stack.svc._seat_local_member(
        space, mem.user_id, role=SpaceRole.MEMBER, actor_user_id=mem.user_id
    )
    with patch.object(
        SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
    ) as rotate:
        await stack.svc.remove_member(
            space.id, actor_username="owner", user_id=mem.user_id
        )
    rotate.assert_awaited_once_with(space.id)


async def test_following_a_stub_runs_the_age_gate(stack):
    """A protected minor below the space's ``min_age`` is refused before
    any request leaves this household."""
    await _remote_stub(stack)
    fan = await stack.provision("fan")
    gate = MagicMock()
    gate.check_space_age_gate = AsyncMock(side_effect=AgeRestrictedError(16))
    stack.svc.attach_child_protection(gate)
    with pytest.raises(AgeRestrictedError):
        await stack.svc.follow_space(fan.user_id, "sp-remote")
    gate.check_space_age_gate.assert_awaited_once_with("sp-remote", fan.user_id)
    assert _join_request_sends(stack) == []


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


async def _key_at(stack, space_id: str, created_at: str, epoch: int = 0) -> None:
    """A content-key row minted at ``created_at`` (tz-aware ISO, the shape
    the key repo writes)."""
    await stack.db.enqueue(
        "INSERT INTO space_keys(space_id, epoch, content_key_hex, created_at)"
        " VALUES(?, ?, ?, ?)",
        (space_id, epoch, "aa" * 32, created_at),
    )


def _attach_writer_certs(stack) -> None:
    """The real writer-scope comparison: a household rotates on leave only
    when its writer scope weakened."""
    stack.svc.attach_writer_certs(
        SpaceWriterCertService(
            space_repo=stack.spaces,
            remote_member_repo=stack.remote,
            space_key_repo=MagicMock(),
            own_instance_id=stack.iid,
            own_identity_pk=b"\x11" * 32,
        )
    )


async def test_a_read_only_remote_followers_leave_does_not_rotate(stack):
    """A follower without the comment / react switches held no writer
    cert: its leave weakens nothing, so nothing rotates."""
    space = await _hosted(stack)
    _attach_writer_certs(stack)
    await _seat_remote(stack, space.id, U_FAN, SpaceRole.SUBSCRIBER)
    with patch.object(
        SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
    ) as rotate:
        assert await stack.svc.on_remote_member_left(space.id, PEER, U_FAN)
    rotate.assert_not_awaited()
    assert await stack.remote.get(space.id, PEER, U_FAN) is None
    assert PEER not in await stack.spaces.list_member_instances(space.id)


async def test_a_remote_followers_leave_owes_a_rotation(stack):
    """The leave rotates nothing; the space owes one, paid by the next
    rotation or the daily sweep."""
    space = await _hosted(stack)
    await _key_at(stack, space.id, "2026-01-01T00:00:00+00:00")
    _attach_writer_certs(stack)
    await _seat_remote(stack, space.id, "u-fan", SpaceRole.SUBSCRIBER)
    assert await stack.svc.rotation_owed(space.id) is None
    with patch.object(
        SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
    ) as rotate:
        assert await stack.svc.on_remote_member_left(space.id, PEER, "u-fan")
    rotate.assert_not_awaited()
    since = await stack.svc.rotation_owed(space.id)
    assert since is not None and since.tzinfo is not None
    assert datetime.now(UTC) - since < timedelta(minutes=5)


async def test_a_commenting_remote_followers_leave_is_owed_not_rotated(stack):
    """A follower that may comment / react held a ``reader`` writer cert:
    its leave still rotates nothing at once — the rotation is owed and the
    cert dies with the epoch the sweep (or any rotation) starts."""
    space = await _hosted(stack)
    await stack.spaces.save(
        replace(
            space,
            features=replace(space.features, allow_subscriber_comment=True),
        )
    )
    await _key_at(stack, space.id, "2026-01-01T00:00:00+00:00")
    _attach_writer_certs(stack)
    await _seat_remote(stack, space.id, U_FAN, SpaceRole.SUBSCRIBER)
    with patch.object(
        SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
    ) as rotate:
        assert await stack.svc.on_remote_member_left(space.id, PEER, U_FAN)
    rotate.assert_not_awaited()
    assert await stack.svc.rotation_owed(space.id) is not None


async def test_a_remote_members_leave_rotates_at_once(stack):
    space = await _hosted(stack)
    _attach_writer_certs(stack)
    await _seat_remote(stack, space.id, "u-mem", SpaceRole.MEMBER)
    with patch.object(
        SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
    ) as rotate:
        assert await stack.svc.on_remote_member_left(space.id, PEER, "u-mem")
    rotate.assert_awaited_once_with(space.id)


async def test_a_mixed_member_and_follower_leave_rotates(stack):
    space = await _hosted(stack)
    _attach_writer_certs(stack)
    await _seat_remote(stack, space.id, U_FAN, SpaceRole.SUBSCRIBER)
    await _seat_remote(stack, space.id, "u-mem", SpaceRole.MEMBER)
    with patch.object(
        SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
    ) as rotate:
        assert await stack.svc.on_remote_member_left(space.id, PEER, None)
    rotate.assert_awaited_once_with(space.id)


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
    await _seat_remote(stack, space.id, U_FAN, SpaceRole.MEMBER)
    with (
        patch.object(
            SpaceService, "rotate_if_writer_scope_weakened", AsyncMock()
        ) as rotate,
        patch.object(
            SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
        ) as full_rotate,
    ):
        assert await stack.svc.on_remote_member_left(space.id, PEER, U_FAN)
        assert rotate.await_count == 1

        assert await _approve_follow(stack, space.id) is True
        token = (await _token_row(stack, space.id))["token"]
        ack, reason, reservation = await _redeemer(stack)._consume_seat_and_build_ack(
            token=token,
            redeemer_instance_id=PEER,
            redeemer_user_id=U_FAN,
            redeemer_identity_pk=PEER_PK,
            redeemer_anchor="fan",
            redeemer_pk=None,
            redeemer_display="Fan",
        )
        assert reason is None and ack is not None and reservation is not None
        assert ack["role"] == SpaceRole.SUBSCRIBER.value
        seat = await stack.remote.get(space.id, PEER, U_FAN)
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


# ── P5: removal owes, ban and followers-off rotate at once ───────────────


async def test_an_admin_removing_a_remote_follower_owes_the_rotation(stack):
    space = await _hosted(stack)
    await _key_at(stack, space.id, "2026-01-01T00:00:00+00:00")
    await _seat_remote(stack, space.id, "u-fan", SpaceRole.SUBSCRIBER)
    with patch.object(
        SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
    ) as rotate:
        await stack.svc.remove_remote_member(
            space.id, actor_username="owner", instance_id=PEER, user_id="u-fan"
        )
    rotate.assert_not_awaited()
    assert await stack.remote.get(space.id, PEER, "u-fan") is None
    assert await stack.svc.rotation_owed(space.id) is not None


async def test_an_admin_removing_a_remote_member_rotates_at_once(stack):
    space = await _hosted(stack)
    await _seat_remote(stack, space.id, "u-mem", SpaceRole.MEMBER)
    with patch.object(
        SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
    ) as rotate:
        await stack.svc.remove_remote_member(
            space.id, actor_username="owner", instance_id=PEER, user_id="u-mem"
        )
    rotate.assert_awaited_once_with(space.id)


async def test_banning_a_seated_remote_follower_rotates_at_once(stack):
    space = await _hosted(stack)
    await _seat_remote(stack, space.id, "u-fan", SpaceRole.SUBSCRIBER)
    with patch.object(
        SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
    ) as rotate:
        await stack.svc.ban(space.id, actor_username="owner", user_id="u-fan")
    rotate.assert_awaited_once_with(space.id)
    assert await stack.remote.get(space.id, PEER, "u-fan") is None


async def test_banning_a_follower_who_already_left_rotates_the_owed_key(stack):
    """The follower left (rotation owed, their key still current); the ban
    kills that key now rather than waiting for the sweep."""
    space = await _hosted(stack)
    await _key_at(stack, space.id, "2026-01-01T00:00:00+00:00")
    await _seat_remote(stack, space.id, "u-fan", SpaceRole.SUBSCRIBER)
    await stack.svc.on_remote_member_left(space.id, PEER, "u-fan")
    with patch.object(
        SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
    ) as rotate:
        await stack.svc.ban(space.id, actor_username="owner", user_id="u-fan")
    rotate.assert_awaited_once_with(space.id)


async def test_banning_a_follower_whose_old_key_is_already_dead_does_not_rotate(
    stack,
):
    space = await _hosted(stack)
    await _seat_remote(stack, space.id, "u-fan", SpaceRole.SUBSCRIBER)
    await stack.svc.on_remote_member_left(space.id, PEER, "u-fan")
    # A rotation after the leave paid the debt.
    await _key_at(stack, space.id, "2999-01-01T00:00:00+00:00")
    assert await stack.svc.rotation_owed(space.id) is None
    with patch.object(
        SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
    ) as rotate:
        await stack.svc.ban(space.id, actor_username="owner", user_id="u-fan")
    rotate.assert_not_awaited()


async def test_followers_off_ends_every_remote_follower_seat_and_rotates_once(
    stack,
):
    """A seat is a seat: every follower — from a follow, a follower link or
    a GFS — is a member with the read-only role, so turning followers off
    ends them all. Each household is told, its seat tombstoned and gossiped,
    its ``space_instances`` row dropped with its last seat; a household
    that also holds a member's seat stays; the key rotates ONCE."""
    space = await _hosted(stack)
    await _seat_remote(stack, space.id, "u-fan", SpaceRole.SUBSCRIBER)
    await stack.remote.add(
        space_id=space.id,
        instance_id="other-house",
        user_id="u-fan2",
        user_pk=None,
        display_name="fan2",
        role=SpaceRole.SUBSCRIBER.value,
    )
    await stack.remote.add(
        space_id=space.id,
        instance_id="other-house",
        user_id="u-mem",
        user_pk=None,
        display_name="mem",
        role=SpaceRole.MEMBER.value,
    )
    await stack.spaces.add_space_instance(space.id, "other-house")
    stack.fed.send_with_mesh_fallback.reset_mock()
    with (
        patch.object(
            SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
        ) as rotate,
        patch.object(SpaceService, "_emit_member_roster_gossip", AsyncMock()) as gossip,
    ):
        await stack.svc.update_config(
            space.id,
            actor_username="owner",
            features=replace(space.features, allow_subscribers=False),
        )
    rotate.assert_awaited_once_with(space.id)
    assert await stack.remote.get(space.id, PEER, "u-fan") is None
    assert await stack.remote.get(space.id, "other-house", "u-fan2") is None
    assert await stack.remote.get(space.id, "other-house", "u-mem") is not None
    instances = await stack.spaces.list_member_instances(space.id)
    assert PEER not in instances and "other-house" in instances
    told = sorted(
        (c.kwargs["to_instance_id"], c.kwargs["payload"]["user_id"])
        for c in stack.fed.send_with_mesh_fallback.await_args_list
        if c.kwargs["event_type"] is FederationEventType.SPACE_REMOTE_MEMBER_REMOVED
    )
    assert told == [("other-house", "u-fan2"), (PEER, "u-fan")]
    ended = sorted(
        c.kwargs["user_id"] for c in gossip.await_args_list if c.kwargs["tombstoned"]
    )
    assert ended == ["u-fan", "u-fan2"]


async def test_followers_off_with_comment_off_too_still_rotates_once(stack):
    space = await _hosted(stack)
    await stack.spaces.save(
        replace(
            space,
            features=replace(space.features, allow_subscriber_comment=True),
        )
    )
    space = await stack.spaces.get(space.id)
    _attach_writer_certs(stack)
    await _seat_remote(stack, space.id, "u-fan", SpaceRole.SUBSCRIBER)
    with patch.object(
        SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
    ) as rotate:
        await stack.svc.update_config(
            space.id,
            actor_username="owner",
            features=replace(
                space.features,
                allow_subscribers=False,
                allow_subscriber_comment=False,
            ),
        )
    rotate.assert_awaited_once_with(space.id)


# ── P5: paying an owed rotation ─────────────────────────────────────────


async def _owing(stack, *, hours_ago: float):
    """A hosted space whose follower left ``hours_ago`` after the key."""
    space = await _hosted(stack)
    await _key_at(stack, space.id, "2026-01-01T00:00:00+00:00")
    await _seat_remote(stack, space.id, "u-fan", SpaceRole.SUBSCRIBER)
    await stack.svc.on_remote_member_left(space.id, PEER, "u-fan")
    left = (datetime.now(UTC) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    await stack.db.enqueue(
        "UPDATE space_remote_members SET removed_at=? WHERE space_id=?",
        (left, space.id),
    )
    return space


def _rotation_that_mints(stack):
    """A rotation stand-in that writes the new key row, as the real one
    does — so the debt is paid by construction."""

    async def _rotate(space_id):
        await _key_at(stack, space_id, datetime.now(UTC).isoformat(), epoch=1)

    return AsyncMock(side_effect=_rotate)


async def test_an_old_debt_is_paid_once(stack):
    space = await _owing(stack, hours_ago=25)
    assert await stack.svc.list_spaces_owing_rotation() == [
        (space.id, await stack.svc.rotation_owed(space.id))
    ]
    rotate = _rotation_that_mints(stack)
    with patch.object(SpaceService, "_rotate_and_distribute_space_key", rotate):
        assert await stack.svc.pay_owed_rotation(space.id) is True
        assert await stack.svc.rotation_owed(space.id) is None
        assert await stack.svc.pay_owed_rotation(space.id) is False
    assert rotate.await_count == 1
    assert await stack.svc.list_spaces_owing_rotation() == []


async def test_a_young_debt_waits(stack):
    space = await _owing(stack, hours_ago=2)
    with patch.object(
        SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
    ) as rotate:
        assert await stack.svc.pay_owed_rotation(space.id) is False
        assert (
            await stack.svc.pay_owed_rotation(space.id, min_age=timedelta(hours=1))
            is True
        )
    rotate.assert_awaited_once_with(space.id)


async def test_a_household_that_does_not_host_never_pays(stack):
    """Only the owner or a seed-holding acting host rotates: a member
    household's mirror may show the debt too, but its rekey would not be
    authority-signed."""
    space = await _owing(stack, hours_ago=25)
    await stack.db.enqueue(
        "UPDATE spaces SET owner_instance_id=? WHERE id=?", (HOST, space.id)
    )
    with patch.object(
        SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
    ) as rotate:
        assert await stack.svc.pay_owed_rotation(space.id) is False
    rotate.assert_not_awaited()


async def test_a_seed_holding_delegated_admin_pays(stack):
    space = await _owing(stack, hours_ago=25)
    await stack.db.enqueue(
        "UPDATE spaces SET owner_instance_id=? WHERE id=?", (HOST, space.id)
    )
    with (
        patch.object(
            SpaceService,
            "_executes_locally_as_delegated_admin",
            AsyncMock(return_value=True),
        ),
        patch.object(
            SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
        ) as rotate,
    ):
        assert await stack.svc.pay_owed_rotation(space.id) is True
    rotate.assert_awaited_once_with(space.id)


async def test_nothing_owed_for_an_unknown_space(stack):
    assert await stack.svc.rotation_owed("nope") is None
    assert await stack.svc.pay_owed_rotation("nope") is False


async def test_followers_off_on_a_seed_holding_delegated_admin_ends_seats_too(
    stack,
):
    """The acting host that holds the seed applies the switch the way the
    owner does: remote follower seats end, the key rotates once."""
    space = await _hosted(stack)
    await _seat_remote(stack, space.id, "u-fan", SpaceRole.SUBSCRIBER)
    await stack.db.enqueue(
        "UPDATE spaces SET owner_instance_id=? WHERE id=?", (HOST, space.id)
    )
    space = await stack.spaces.get(space.id)
    with (
        patch.object(
            SpaceService,
            "_executes_locally_as_delegated_admin",
            AsyncMock(return_value=True),
        ),
        patch.object(
            SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
        ) as rotate,
    ):
        await stack.svc.update_config(
            space.id,
            actor_username="owner",
            features=replace(space.features, allow_subscribers=False),
        )
    rotate.assert_awaited_once_with(space.id)
    assert await stack.remote.get(space.id, PEER, "u-fan") is None
