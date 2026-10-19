"""Local communities in ``SpaceService`` — the area on create / settings and
every seat-granting path (owner decisions 2026-10-19).

A public space bound to a centre + radius admits a household only while its
home — rounded to ~1 km and carried by the join — lies inside: clearly
inside proceeds, clearly outside is refused, near the edge always goes to
the admins (even in open mode, answered like any request); an admin's
approval seats; members stay when the area changes later.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import SpaceLocalAreaChanged
from socialhome.domain.federation import PairingStatus
from socialhome.domain.local_community import LocalArea
from socialhome.domain.space import (
    HomeNotSetError,
    JoinMode,
    JoinRequestSentError,
    LocalAreaDelegatedError,
    LocalAreaPublicOnlyError,
    LocalAreaRateLimitedError,
    LocalAreaUnavailableError,
    LocationRequiredError,
    OutsideAreaError,
    SpaceFeatures,
    SpacePermissionError,
    SpaceRole,
    SpaceType,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.local_area_attempt_repo import (
    SqliteLocalAreaAttemptRepo,
)
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.local_community import (
    MAX_LOCAL_AREA_CHECKS_PER_PERSON,
    LocalAreaBudget,
)
from socialhome.services.space_service import (
    SpaceService,
    build_space_snapshot_for_federation,
)
from socialhome.services.user_service import UserService

ZURICH = (47.3769, 8.5417)
NEAR = (47.3920, 8.5600)  # ~2 km from the centre
EDGE = (47.3769, 8.6082)  # ~5 km east: its ~1 km cell straddles the rim
BERN = (46.9481, 7.4474)  # ~95 km away


class _FedRepo:
    """Our own home (the only home the host ever reads), and the peers a
    joiner-side request needs to be CONFIRMED."""

    def __init__(self, own: tuple | None, peers: dict | None = None) -> None:
        self.own = own
        self.peers = peers or {}

    async def get_local_identity(self):
        lat, lon = self.own or (None, None)
        return {"instance_id": "self", "home_lat": lat, "home_lon": lon}

    async def get_instance(self, iid):
        return self.peers.get(iid)


@pytest.fixture
async def stack(tmp_dir):
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
    space_repo = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x09" * 32))
    users = UserService(user_repo, bus, own_instance_public_key=kp.public_key)
    svc = SpaceService(
        space_repo, SqliteSpacePostRepo(db), user_repo, bus, own_instance_id=iid
    )
    svc._federation_repo = _FedRepo(own=NEAR)
    svc.attach_local_area_budget(LocalAreaBudget(SqliteLocalAreaAttemptRepo(db)))

    class S:
        pass

    s = S()
    s.svc, s.repo, s.bus, s.iid, s.db = svc, space_repo, bus, iid, db
    s.anna = await users.provision(username="anna", display_name="Anna")
    s.ben = await users.provision(username="ben", display_name="Ben")
    s.carl = await users.provision(username="carl", display_name="Carl")
    yield s
    await db.shutdown()


async def _community(s, *, join_mode=JoinMode.OPEN, **kw):
    space = await s.svc.create_space(
        owner_username="anna",
        name="Quartier",
        space_type=SpaceType.PUBLIC,
        join_mode=join_mode,
        lat=ZURICH[0],
        lon=ZURICH[1],
        radius_km=5,
        **kw,
    )
    return space


def _home(s, own=None):
    s.svc._federation_repo = _FedRepo(own=own)


async def _pending(s, space_id):
    return await s.repo.list_pending_join_requests(space_id)


# ── the area on create ──────────────────────────────────────────────────


async def test_create_stores_the_area_at_4dp(stack):
    space = await stack.svc.create_space(
        owner_username="anna",
        name="Quartier",
        space_type=SpaceType.PUBLIC,
        lat=47.376_912_3,
        lon=8.541_749_9,
        radius_km=2,
    )
    got = await stack.repo.get(space.id)
    assert (got.lat, got.lon, got.radius_km) == (47.3769, 8.5417, 2.0)


async def test_creator_is_seated_whatever_their_home(stack):
    """Creating is not joining: the owner is seated even when this
    household's home is outside (or unknown)."""
    _home(stack, own=BERN)
    space = await _community(stack)
    member = await stack.repo.get_member(space.id, stack.anna.user_id)
    assert member is not None and member.role == SpaceRole.OWNER


@pytest.mark.parametrize("tier", [SpaceType.PRIVATE, SpaceType.HOUSEHOLD])
async def test_create_refuses_an_area_on_another_tier(stack, tier):
    with pytest.raises(LocalAreaPublicOnlyError) as exc:
        await stack.svc.create_space(
            owner_username="anna", name="X", space_type=tier, lat=1, lon=2, radius_km=5
        )
    assert exc.value.status == 422


@pytest.mark.parametrize(
    "kw",
    [
        {"lat": 1.0, "lon": 2.0},
        {"radius_km": 5},
        {"lat": 1.0, "lon": 2.0, "radius_km": 0.1},
        {"lat": 1.0, "lon": 2.0, "radius_km": 80},
        {"lat": 100.0, "lon": 2.0, "radius_km": 5},
        {"lat": "1", "lon": 2.0, "radius_km": 5},
    ],
)
async def test_create_refuses_a_partial_or_bad_area(stack, kw):
    with pytest.raises(ValueError):
        await stack.svc.create_space(
            owner_username="anna", name="X", space_type=SpaceType.PUBLIC, **kw
        )
    assert await stack.repo.list_by_type(SpaceType.PUBLIC) == []


# ── set_local_area ──────────────────────────────────────────────────────


async def test_owner_sets_moves_and_clears_the_area(stack):
    seen: list[SpaceLocalAreaChanged] = []

    async def _on(e):
        seen.append(e)

    stack.bus.subscribe(SpaceLocalAreaChanged, _on)
    space = await stack.svc.create_space(
        owner_username="anna", name="Q", space_type=SpaceType.PUBLIC
    )
    seq = (await stack.repo.get(space.id)).config_sequence
    area = await stack.svc.set_local_area(
        space.id, actor_username="anna", lat=46.94812, lon=7.44741, radius_km=10
    )
    assert area == LocalArea(46.9481, 7.4474, 10.0)
    got = await stack.repo.get(space.id)
    assert (got.lat, got.lon, got.radius_km) == (46.9481, 7.4474, 10.0)
    # Host-local: no config bump.
    assert got.config_sequence == seq
    assert (
        await stack.svc.set_local_area(
            space.id, actor_username="anna", lat=None, lon=None, radius_km=None
        )
        is None
    )
    got = await stack.repo.get(space.id)
    assert (got.lat, got.lon, got.radius_km) == (None, None, None)
    assert len(seen) == 2


async def test_a_member_cannot_set_the_area(stack):
    space = await _community(stack)
    await stack.svc.request_join(space.id, user_id=stack.ben.user_id)
    with pytest.raises(SpacePermissionError):
        await stack.svc.set_local_area(
            space.id, actor_username="ben", lat=None, lon=None, radius_km=None
        )


async def test_the_area_is_the_hosts(stack):
    space = await _community(stack)
    await stack.db.enqueue(
        "UPDATE spaces SET owner_instance_id='elsewhere' WHERE id=?", (space.id,)
    )
    with pytest.raises(LocalAreaUnavailableError) as exc:
        await stack.svc.set_local_area(
            space.id, actor_username="anna", lat=1, lon=2, radius_km=5
        )
    assert exc.value.status == 409


async def test_set_area_needs_the_public_tier(stack):
    space = await stack.svc.create_space(owner_username="anna", name="P")
    with pytest.raises(LocalAreaPublicOnlyError):
        await stack.svc.set_local_area(
            space.id, actor_username="anna", lat=1, lon=2, radius_km=5
        )
    # Clearing works on any tier.
    await stack.svc.set_local_area(
        space.id, actor_username="anna", lat=None, lon=None, radius_km=None
    )


async def test_area_and_delegated_admin_authority_never_go_together(stack):
    space = await stack.svc.create_space(
        owner_username="anna",
        name="Q",
        space_type=SpaceType.PUBLIC,
        features=SpaceFeatures(delegated_admin_authority=True),
    )
    with pytest.raises(LocalAreaDelegatedError) as exc:
        await stack.svc.set_local_area(
            space.id, actor_username="anna", lat=1, lon=2, radius_km=5
        )
    assert exc.value.status == 409
    with pytest.raises(LocalAreaDelegatedError):
        await _community(stack, features=SpaceFeatures(delegated_admin_authority=True))
    community = await _community(stack)
    with pytest.raises(LocalAreaDelegatedError):
        await stack.svc.update_config(
            community.id,
            actor_username="anna",
            features=SpaceFeatures(delegated_admin_authority=True),
        )


async def test_leaving_the_public_tier_drops_the_area(stack):
    space = await _community(stack)
    await stack.svc.update_config(
        space.id, actor_username="anna", space_type=SpaceType.PRIVATE
    )
    got = await stack.repo.get(space.id)
    assert (got.lat, got.lon, got.radius_km) == (None, None, None)


# ── local users: every seat path ────────────────────────────────────────


async def test_open_join_inside_is_seated(stack):
    space = await _community(stack)
    assert await stack.svc.request_join(space.id, user_id=stack.ben.user_id) is None
    assert await stack.repo.get_member(space.id, stack.ben.user_id) is not None


@pytest.mark.parametrize("own,err", [(BERN, OutsideAreaError), (None, HomeNotSetError)])
@pytest.mark.parametrize("mode", [JoinMode.OPEN, JoinMode.REQUEST])
async def test_join_and_request_refused_outside_or_without_a_home(
    stack, own, err, mode
):
    space = await _community(stack, join_mode=mode)
    _home(stack, own=own)
    with pytest.raises(err) as exc:
        await stack.svc.request_join(space.id, user_id=stack.ben.user_id)
    assert exc.value.status == 403
    assert exc.value.params == {"whose": "own"}
    assert await stack.repo.get_member(space.id, stack.ben.user_id) is None
    assert await _pending(stack, space.id) == []


async def test_near_the_edge_even_an_open_space_files_a_request(stack):
    """Never decided automatically: the answer is a pending request — the
    same thing a request-mode space answers — and the admins see why."""
    space = await _community(stack)
    _home(stack, own=EDGE)
    rid = await stack.svc.request_join(space.id, user_id=stack.ben.user_id)
    assert isinstance(rid, str)
    assert await stack.repo.get_member(space.id, stack.ben.user_id) is None
    (row,) = await _pending(stack, space.id)
    assert row["id"] == rid
    assert row["area_verdict"] == "edge"
    # Nothing about the home is kept with it.
    assert not {"lat", "lon", "home_lat", "home_lon"} & set(row)


async def test_the_requester_cannot_tell_edge_from_request_mode(stack):
    """An edge request in an open space and an inside request in a
    request-mode space answer the person identically."""
    open_space = await _community(stack)
    request_space = await _community(stack, join_mode=JoinMode.REQUEST)
    _home(stack, own=EDGE)
    edge_answer = await stack.svc.request_join(open_space.id, user_id=stack.ben.user_id)
    _home(stack, own=NEAR)
    normal_answer = await stack.svc.request_join(
        request_space.id, user_id=stack.ben.user_id
    )
    assert type(edge_answer) is type(normal_answer) is str
    assert len(edge_answer) == len(normal_answer)
    (normal_row,) = await _pending(stack, request_space.id)
    assert normal_row["area_verdict"] == "inside"


async def test_an_admin_approval_seats_an_edge_request(stack):
    """The approval is the decision: nothing is re-checked (the point was
    never kept), even when the home has moved since."""
    space = await _community(stack)
    _home(stack, own=EDGE)
    rid = await stack.svc.request_join(space.id, user_id=stack.ben.user_id)
    _home(stack, own=BERN)
    member = await stack.svc.approve_join_request(rid, actor_username="anna")
    assert member is not None
    assert await stack.repo.get_member(space.id, stack.ben.user_id) is not None


async def test_follow_inside_outside_and_near_the_edge(stack):
    space = await _community(stack, features=SpaceFeatures(allow_subscribers=True))
    _home(stack, own=BERN)
    with pytest.raises(OutsideAreaError):
        await stack.svc.subscribe_to_space(stack.ben.user_id, space.id)
    _home(stack, own=EDGE)
    with pytest.raises(JoinRequestSentError) as exc:
        await stack.svc.subscribe_to_space(stack.ben.user_id, space.id)
    assert exc.value.code == "JOIN_REQUEST_SENT"
    rid = exc.value.params["request_id"]
    assert [r["id"] for r in await _pending(stack, space.id)] == [rid]
    # Asking again answers with the same request — no second row or check.
    with pytest.raises(JoinRequestSentError) as again:
        await stack.svc.subscribe_to_space(stack.ben.user_id, space.id)
    assert again.value.params["request_id"] == rid
    assert len(await _pending(stack, space.id)) == 1
    _home(stack, own=NEAR)
    await stack.svc.subscribe_to_space(stack.carl.user_id, space.id)
    seat = await stack.repo.get_member(space.id, stack.carl.user_id)
    assert seat is not None and seat.role == SpaceRole.SUBSCRIBER


async def test_an_admins_direct_add_is_refused_outside_and_decides_the_edge(stack):
    space = await _community(stack)
    _home(stack, own=BERN)
    with pytest.raises(OutsideAreaError):
        await stack.svc.add_member(
            space.id, actor_username="anna", user_id=stack.ben.user_id
        )
    _home(stack, own=EDGE)
    await stack.svc.add_member(
        space.id, actor_username="anna", user_id=stack.ben.user_id
    )
    assert await stack.repo.get_member(space.id, stack.ben.user_id) is not None


async def test_local_invite_checked_when_sent_and_reviewed_at_the_edge(stack):
    space = await _community(stack)
    _home(stack, own=None)
    with pytest.raises(HomeNotSetError):
        await stack.svc.invite_local_user(
            space.id, actor_username="anna", user_id=stack.ben.user_id
        )
    _home(stack, own=NEAR)
    inv = await stack.svc.invite_local_user(
        space.id, actor_username="anna", user_id=stack.ben.user_id
    )
    _home(stack, own=BERN)
    with pytest.raises(OutsideAreaError):
        await stack.svc.accept_local_invite(inv, user_id=stack.ben.user_id)
    _home(stack, own=EDGE)
    with pytest.raises(JoinRequestSentError):
        await stack.svc.accept_local_invite(inv, user_id=stack.ben.user_id)
    assert (await stack.repo.get_invitation(inv))["status"] == "pending"
    assert await stack.repo.get_member(space.id, stack.ben.user_id) is None
    assert len(await _pending(stack, space.id)) == 1


async def test_an_invite_link_is_no_override_and_the_use_is_handed_back(stack):
    space = await _community(stack)
    token = await stack.svc.create_invite_token(space.id, actor_username="anna", uses=1)
    _home(stack, own=BERN)
    with pytest.raises(OutsideAreaError):
        await stack.svc.accept_invite_token(token, user_id=stack.ben.user_id)
    _home(stack, own=EDGE)
    with pytest.raises(JoinRequestSentError):
        await stack.svc.accept_invite_token(token, user_id=stack.ben.user_id)
    assert await stack.repo.get_member(space.id, stack.ben.user_id) is None
    assert len(await _pending(stack, space.id)) == 1
    # The one use came back both times: Carl, inside, still gets in.
    _home(stack, own=NEAR)
    await stack.svc.accept_invite_token(token, user_id=stack.carl.user_id)
    assert await stack.repo.get_member(space.id, stack.carl.user_id) is not None


async def test_existing_members_stay_when_the_area_moves(stack):
    space = await _community(stack)
    await stack.svc.request_join(space.id, user_id=stack.ben.user_id)
    await stack.svc.set_local_area(
        space.id, actor_username="anna", lat=BERN[0], lon=BERN[1], radius_km=1
    )
    assert await stack.repo.get_member(space.id, stack.ben.user_id) is not None
    # …but nobody new from outside the new area gets in.
    with pytest.raises(OutsideAreaError):
        await stack.svc.request_join(space.id, user_id=stack.carl.user_id)


async def test_a_space_without_an_area_is_unchanged(stack):
    """No area → no home needed, as before."""
    _home(stack, own=None)
    space = await stack.svc.create_space(
        owner_username="anna",
        name="Plain",
        space_type=SpaceType.PUBLIC,
        join_mode=JoinMode.OPEN,
    )
    assert await stack.svc.request_join(space.id, user_id=stack.ben.user_id) is None


async def test_an_ordinary_open_space_seats_despite_a_stale_request(stack):
    """The pending-request shortcut is for local communities: an open space
    without an area seats as it always did, even when an old request (filed
    while it took requests) still waits."""
    space = await stack.svc.create_space(
        owner_username="anna",
        name="Plain",
        space_type=SpaceType.PUBLIC,
        join_mode=JoinMode.OPEN,
    )
    await stack.repo.save_join_request(space.id, stack.ben.user_id)
    assert await stack.svc.request_join(space.id, user_id=stack.ben.user_id) is None
    assert await stack.repo.get_member(space.id, stack.ben.user_id) is not None


# ── remote households (host side) ───────────────────────────────────────


async def test_a_remote_approval_mints_the_token_without_a_recheck(stack):
    """The approval is the decision; the APPROVED rides the mesh when the
    applicant is not paired with us (a link or invite join)."""
    space = await _community(stack, join_mode=JoinMode.REQUEST)
    _home(stack, own=None)  # nothing about any home is consulted
    fed = MagicMock()
    fed.send_with_mesh_fallback = AsyncMock()
    stack.svc._federation = fed
    rid = await stack.repo.save_join_request(
        space.id, "u-x", remote_applicant_instance_id="peer", area_verdict="edge"
    )
    assert await stack.svc.approve_join_request(rid, actor_username="anna") is None
    fed.send_with_mesh_fallback.assert_awaited_once()
    assert await stack.repo.has_approved_join_request(
        space.id, "u-x", remote_applicant_instance_id="peer", within_days=7
    )


async def test_an_invitation_snapshot_names_the_radius_never_the_centre(stack):
    space = await _community(stack)
    meta = await build_space_snapshot_for_federation(
        await stack.repo.get(space.id),
        space_repo=stack.repo,
        remote_member_repo=None,
        user_repo=stack.svc._users,
        own_instance_id=stack.iid,
    )
    assert meta["local_community"] == {"radius_km": 5.0}
    flat = repr(meta)
    assert str(ZURICH[0]) not in flat and str(ZURICH[1]) not in flat


# ── the joiner's side ───────────────────────────────────────────────────


def _peer_confirmed():
    return SimpleNamespace(status=PairingStatus.CONFIRMED, home_lat=None, home_lon=None)


async def test_a_remote_request_carries_the_coarse_home_only_with_consent(stack):
    stack.svc._federation_repo = _FedRepo(
        own=(47.376_912, 8.541_749), peers={"host-b": _peer_confirmed()}
    )
    fed = MagicMock()
    fed.send_event = AsyncMock()
    stack.svc._federation = fed
    await stack.svc.request_join_remote(
        "sp-b", applicant_user_id=stack.ben.user_id, host_instance_id="host-b"
    )
    assert "join_location" not in fed.send_event.await_args.kwargs["payload"]
    await stack.svc.request_join_remote(
        "sp-b",
        applicant_user_id=stack.carl.user_id,
        host_instance_id="host-b",
        share_location=True,
    )
    payload = fed.send_event.await_args.kwargs["payload"]
    assert payload["join_location"] == {"lat": 47.38, "lon": 8.54}
    # The applicant keeps its side of the request for a space it does not
    # hold (no ``spaces`` row here — migration 0111 dropped the FK).
    rows = await stack.repo.list_pending_join_request_space_ids_for_user(
        stack.carl.user_id
    )
    assert rows == ["sp-b"]


async def test_consent_without_a_home_sends_nothing(stack):
    stack.svc._federation_repo = _FedRepo(own=None, peers={"host-b": _peer_confirmed()})
    fed = MagicMock()
    fed.send_event = AsyncMock()
    stack.svc._federation = fed
    with pytest.raises(HomeNotSetError):
        await stack.svc.request_join_remote(
            "sp-b",
            applicant_user_id=stack.ben.user_id,
            host_instance_id="host-b",
            share_location=True,
        )
    fed.send_event.assert_not_awaited()


async def test_a_remote_redeem_carries_the_coarse_home_only_with_consent(stack):
    _home(stack, own=(47.376_912, 8.541_749))
    coordinator = MagicMock()
    coordinator.request_redeem = AsyncMock(
        return_value={"space_id": "x", "role": "member"}
    )
    stack.svc._redeem_coordinator = coordinator
    await stack.svc.redeem_invite_token(
        "tok", user_id=stack.ben.user_id, issuer_instance_id="host-b"
    )
    assert coordinator.request_redeem.await_args.kwargs["join_location"] is None
    await stack.svc.redeem_invite_token(
        "tok",
        user_id=stack.ben.user_id,
        issuer_instance_id="host-b",
        share_location=True,
    )
    assert coordinator.request_redeem.await_args.kwargs["join_location"] == {
        "lat": 47.38,
        "lon": 8.54,
    }


# ── the daily per-person check budget ───────────────────────────────────


async def test_joining_and_leaving_spends_the_persons_budget(stack):
    """Every open join is a check Ben starts — successful ones too. Past ten
    that day it is 429; Carl, in the same household, is not held off."""
    space = await _community(stack)
    for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON):
        assert await stack.svc.request_join(space.id, user_id=stack.ben.user_id) is None
        await stack.repo.delete_member(space.id, stack.ben.user_id)
    with pytest.raises(LocalAreaRateLimitedError) as exc:
        await stack.svc.request_join(space.id, user_id=stack.ben.user_id)
    assert (exc.value.status, exc.value.code) == (429, "LOCAL_AREA_RATE_LIMITED")
    assert exc.value.params == {"whose": "own"}
    assert await stack.repo.get_member(space.id, stack.ben.user_id) is None
    assert await stack.svc.request_join(space.id, user_id=stack.carl.user_id) is None


@pytest.mark.parametrize("mode,own", [(JoinMode.REQUEST, NEAR), (JoinMode.OPEN, EDGE)])
async def test_asking_again_while_pending_returns_that_request(stack, mode, own):
    """A re-request of a pending request — a normal one, or one an edge home
    turned into a request in an open space — is answered by it: no second
    row, and no budget spent."""
    space = await _community(stack, join_mode=mode)
    _home(stack, own=own)
    first = await stack.svc.request_join(space.id, user_id=stack.ben.user_id)
    assert first is not None
    for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON + 2):
        again = await stack.svc.request_join(space.id, user_id=stack.ben.user_id)
        assert again == first
    assert [r["id"] for r in await _pending(stack, space.id)] == [first]


async def test_an_admins_add_and_approval_spend_nothing(stack):
    """Admin-started steps are the admin's, not the joining person's."""
    space = await _community(stack, join_mode=JoinMode.REQUEST)
    rid = await stack.svc.request_join(space.id, user_id=stack.ben.user_id)
    await stack.svc.approve_join_request(rid, actor_username="anna")
    await stack.repo.delete_member(space.id, stack.ben.user_id)
    for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON):
        await stack.svc.add_member(
            space.id, actor_username="anna", user_id=stack.carl.user_id
        )
        await stack.repo.delete_member(space.id, stack.carl.user_id)
    # Ben spent one (the request); nine left.
    for _ in range(MAX_LOCAL_AREA_CHECKS_PER_PERSON - 1):
        assert await stack.svc.request_join(space.id, user_id=stack.ben.user_id)
        for row in await _pending(stack, space.id):
            await stack.repo.update_join_request_status(row["id"], "withdrawn")
    with pytest.raises(LocalAreaRateLimitedError):
        await stack.svc.request_join(space.id, user_id=stack.ben.user_id)


async def test_a_remote_join_without_consent_is_refused_by_the_snapshot(stack):
    """Sanity: LocationRequiredError is a coded 403 the SPA turns into the
    consent prompt."""
    err = LocationRequiredError(params={"whose": "host"})
    assert (err.status, err.code) == (403, "LOCATION_REQUIRED")
