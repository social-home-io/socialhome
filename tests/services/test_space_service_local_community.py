"""Local communities in ``SpaceService`` — the area on create / settings and
every seat-granting path of the host (owner decisions 2026-10-19).

A public space bound to a centre + radius admits a household only while the
home it shares with the host lies inside: no override for an admin's
invite, an invite link or a direct add; approval re-checks; members stay
when the area changes later.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import SpaceLocalAreaChanged
from socialhome.domain.local_community import LocalArea
from socialhome.domain.space import (
    HomeNotSharedError,
    JoinMode,
    LocalAreaDelegatedError,
    LocalAreaPublicOnlyError,
    LocalAreaUnavailableError,
    OutsideAreaError,
    SpaceFeatures,
    SpacePermissionError,
    SpaceRole,
    SpaceType,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_service import SpaceService
from socialhome.services.user_service import UserService

ZURICH = (47.3769, 8.5417)
NEAR = (47.3920, 8.5600)  # ~2 km from the centre
BERN = (46.9481, 7.4474)  # ~95 km away


@dataclass
class _Peer:
    home_lat: float | None
    home_lon: float | None


class _FedRepo:
    """The two reads the local-community check makes."""

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


def _home(s, own=None, **peers):
    s.svc._federation_repo = _FedRepo(
        own=own, peers={k: _Peer(*v) for k, v in peers.items()}
    )


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


@pytest.mark.parametrize(
    "own,err", [(BERN, OutsideAreaError), (None, HomeNotSharedError)]
)
@pytest.mark.parametrize("mode", [JoinMode.OPEN, JoinMode.REQUEST])
async def test_join_and_request_refused_outside_or_unknown(stack, own, err, mode):
    space = await _community(stack, join_mode=mode)
    _home(stack, own=own)
    with pytest.raises(err) as exc:
        await stack.svc.request_join(space.id, user_id=stack.ben.user_id)
    assert exc.value.status == 403
    assert exc.value.params == {"whose": "own"}
    assert await stack.repo.get_member(space.id, stack.ben.user_id) is None
    assert await stack.repo.list_pending_join_requests(space.id) == []


async def test_approval_rechecks_and_leaves_the_request_pending(stack):
    space = await _community(stack, join_mode=JoinMode.REQUEST)
    rid = await stack.svc.request_join(space.id, user_id=stack.ben.user_id)
    assert rid is not None
    # The household moved away before the admin got to it.
    _home(stack, own=BERN)
    with pytest.raises(OutsideAreaError):
        await stack.svc.approve_join_request(rid, actor_username="anna")
    pending = await stack.repo.list_pending_join_requests(space.id)
    assert [r["id"] for r in pending] == [rid]
    assert await stack.repo.get_member(space.id, stack.ben.user_id) is None
    _home(stack, own=NEAR)
    await stack.svc.approve_join_request(rid, actor_username="anna")
    assert await stack.repo.get_member(space.id, stack.ben.user_id) is not None


async def test_follow_is_gated_like_a_join(stack):
    space = await _community(stack, features=SpaceFeatures(allow_subscribers=True))
    _home(stack, own=BERN)
    with pytest.raises(OutsideAreaError):
        await stack.svc.subscribe_to_space(stack.ben.user_id, space.id)
    _home(stack, own=NEAR)
    await stack.svc.subscribe_to_space(stack.ben.user_id, space.id)
    seat = await stack.repo.get_member(space.id, stack.ben.user_id)
    assert seat is not None and seat.role == SpaceRole.SUBSCRIBER


async def test_no_override_for_an_admins_direct_add(stack):
    space = await _community(stack)
    _home(stack, own=BERN)
    with pytest.raises(OutsideAreaError):
        await stack.svc.add_member(
            space.id, actor_username="anna", user_id=stack.ben.user_id
        )


async def test_local_invite_checked_when_sent_and_when_accepted(stack):
    space = await _community(stack)
    _home(stack, own=None)
    with pytest.raises(HomeNotSharedError):
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
    assert (await stack.repo.get_invitation(inv))["status"] == "pending"


async def test_an_invite_link_is_no_override_and_the_use_is_handed_back(stack):
    space = await _community(stack)
    token = await stack.svc.create_invite_token(space.id, actor_username="anna", uses=1)
    _home(stack, own=BERN)
    with pytest.raises(OutsideAreaError):
        await stack.svc.accept_invite_token(token, user_id=stack.ben.user_id)
    assert await stack.repo.get_member(space.id, stack.ben.user_id) is None
    # The one use came back: the same link works once the home is inside.
    _home(stack, own=NEAR)
    await stack.svc.accept_invite_token(token, user_id=stack.ben.user_id)
    assert await stack.repo.get_member(space.id, stack.ben.user_id) is not None


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


# ── remote households (host side) ───────────────────────────────────────


async def test_remote_join_request_approval_checks_the_peers_shared_home(stack):
    space = await _community(stack, join_mode=JoinMode.REQUEST)
    _home(stack, own=NEAR, peer_out=BERN, peer_hidden=(None, None))
    for peer, err in (
        ("peer_out", OutsideAreaError),
        ("peer_hidden", HomeNotSharedError),
    ):
        rid = await stack.repo.save_join_request(
            space.id, f"u-{peer}", remote_applicant_instance_id=peer
        )
        with pytest.raises(err) as exc:
            await stack.svc.approve_join_request(rid, actor_username="anna")
        assert exc.value.params == {"whose": "peer"}
    # Both still pending: nothing minted, nothing sent.
    assert len(await stack.repo.list_pending_join_requests(space.id)) == 2


async def test_remote_join_request_approval_inside_mints_the_token(stack):
    space = await _community(stack, join_mode=JoinMode.REQUEST)
    _home(stack, own=NEAR, peer_in=NEAR)
    fed = MagicMock()
    fed.send_event = AsyncMock()
    stack.svc._federation = fed
    rid = await stack.repo.save_join_request(
        space.id, "u-in", remote_applicant_instance_id="peer_in"
    )
    assert await stack.svc.approve_join_request(rid, actor_username="anna") is None
    fed.send_event.assert_awaited_once()


async def test_direct_remote_invite_refused_before_anything_is_minted(stack):
    space = await _community(stack)
    _home(stack, own=NEAR, peer_out=BERN)
    fed = MagicMock()
    fed.send_with_mesh_fallback = AsyncMock()
    stack.svc._federation = fed
    with pytest.raises(OutsideAreaError):
        await stack.svc.invite_remote_user(
            space.id,
            actor_username="anna",
            invitee_instance_id="peer_out",
            invitee_user_id="u-x",
        )
    fed.send_with_mesh_fallback.assert_not_awaited()
    assert await stack.repo.list_live_invite_tokens(space.id) == []
