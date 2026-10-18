"""Frames about a space's people never reach its followers (owner decision
13): approval proposals and zones go to the members only, like the
``GET …/proposals`` / ``…/zones`` reads they keep live."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from socialhome.domain.events import (
    SpaceMemberJoined,
    SpaceMemberLeft,
    SpaceMemberProfileUpdated,
    SpaceProposalUpdated,
    SpaceZoneDeleted,
    SpaceZoneUpserted,
)
from socialhome.domain.space import SpaceMember, SpaceRole
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.ws_manager import WebSocketManager
from socialhome.services.realtime_service import RealtimeService


class _FakeUserRepo:
    async def list_active(self):
        return []


class _FakeSpaceRepo:
    def __init__(self, members: list[SpaceMember]) -> None:
        self._members = members

    async def list_members(self, space_id):
        return [m for m in self._members if m.space_id == space_id]

    async def list_local_member_user_ids(self, space_id):
        return [m.user_id for m in await self.list_members(space_id)]

    async def get_member(self, space_id, user_id):
        return next(
            (m for m in await self.list_members(space_id) if m.user_id == user_id),
            None,
        )


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send_str(self, msg):
        self.sent.append(msg)


_ROLES = {
    "own": SpaceRole.OWNER,
    "adm": SpaceRole.ADMIN,
    "mod": SpaceRole.MODERATOR,
    "mem": SpaceRole.MEMBER,
    "fol": SpaceRole.SUBSCRIBER,
    "fol2": SpaceRole.SUBSCRIBER,
}


@pytest.fixture
async def env():
    bus = EventBus()
    ws = WebSocketManager()
    repo = _FakeSpaceRepo(
        [
            SpaceMember(space_id="sp-1", user_id=uid, role=role, joined_at="2026")
            for uid, role in _ROLES.items()
        ]
    )
    RealtimeService(bus, ws, user_repo=_FakeUserRepo(), space_repo=repo).wire()
    socks = {}
    for uid in _ROLES:
        socks[uid] = _FakeWS()
        await ws.register(uid, socks[uid])
    return bus, socks


_EVENTS = [
    SpaceProposalUpdated(space_id="sp-1", proposal_id="pr-1", view={"id": "pr-1"}),
    SpaceZoneUpserted(
        space_id="sp-1",
        zone_id="z1",
        name="School",
        latitude=47.37,
        longitude=8.54,
        radius_m=150,
        color=None,
        created_by="own",
        updated_at="2026-10-18T00:00:00+00:00",
    ),
    SpaceZoneDeleted(space_id="sp-1", zone_id="z1", deleted_by="own"),
]


@pytest.mark.parametrize("event", _EVENTS, ids=lambda e: type(e).__name__)
async def test_people_frames_skip_followers(env, event):
    bus, socks = env
    await bus.publish(event)
    for uid in ("own", "adm", "mod", "mem"):
        assert len(socks[uid].sent) == 1, uid
    assert socks["fol"].sent == []
    assert socks["fol2"].sent == []


# ─── Roster frames: a follower learns about members, never other followers ──

_ABOUT_A_FOLLOWER = [
    SpaceMemberJoined(space_id="sp-1", user_id="fol2", role=SpaceRole.SUBSCRIBER),
    SpaceMemberLeft(space_id="sp-1", user_id="fol2", role=SpaceRole.SUBSCRIBER),
    # The seat is gone and the role unknown: fail closed — members only.
    SpaceMemberLeft(space_id="sp-1", user_id="gone"),
    SpaceMemberProfileUpdated(
        space_id="sp-1", user_id="fol2", space_display_name="F", picture_hash=None
    ),
]

_ABOUT_A_MEMBER = [
    SpaceMemberJoined(space_id="sp-1", user_id="mem", role=SpaceRole.MEMBER),
    SpaceMemberLeft(space_id="sp-1", user_id="gone", role=SpaceRole.MEMBER),
    SpaceMemberProfileUpdated(
        space_id="sp-1", user_id="mem", space_display_name="M", picture_hash=None
    ),
]


@pytest.mark.parametrize(
    "event", _ABOUT_A_FOLLOWER, ids=lambda e: type(e).__name__ + ":" + e.user_id
)
async def test_frames_about_a_follower_reach_the_members_only(env, event):
    bus, socks = env
    await bus.publish(event)
    for uid in ("own", "adm", "mod", "mem"):
        assert len(socks[uid].sent) == 1, uid
    assert socks["fol"].sent == []


@pytest.mark.parametrize(
    "event", _ABOUT_A_MEMBER, ids=lambda e: type(e).__name__ + ":" + e.user_id
)
async def test_frames_about_a_member_reach_the_followers_too(env, event):
    bus, socks = env
    await bus.publish(event)
    for uid in ("own", "adm", "mod", "mem", "fol", "fol2"):
        assert len(socks[uid].sent) == 1, uid


class _FakeRemoteSeats:
    def __init__(self, seats) -> None:
        self._seats = seats

    async def list_for_space(self, space_id):
        return [s for s in self._seats if s.space_id == space_id]


@pytest.mark.parametrize(
    ("remote_role", "followers_hear"),
    [(SpaceRole.SUBSCRIBER, False), (None, True), (SpaceRole.ADMIN, True)],
)
async def test_a_remote_seats_profile_frame_follows_its_role(
    remote_role, followers_hear
):
    """A seat on another household (``space_remote_members``; a missing
    role reads as a member's, like the roster): a remote follower's profile
    is the members' business only, a remote member's reaches everyone."""
    bus = EventBus()
    ws = WebSocketManager()
    repo = _FakeSpaceRepo(
        [
            SpaceMember(space_id="sp-1", user_id=uid, role=role, joined_at="2026")
            for uid, role in _ROLES.items()
        ]
    )
    remote = _FakeRemoteSeats(
        [SimpleNamespace(space_id="sp-1", user_id="u-remote", role=remote_role)]
    )
    RealtimeService(
        bus, ws, user_repo=_FakeUserRepo(), space_repo=repo, remote_member_repo=remote
    ).wire()
    socks = {uid: _FakeWS() for uid in _ROLES}
    for uid, sock in socks.items():
        await ws.register(uid, sock)
    await bus.publish(
        SpaceMemberProfileUpdated(
            space_id="sp-1",
            user_id="u-remote",
            space_display_name="R",
            picture_hash=None,
            origin_instance_id="peer-1",
        )
    )
    assert len(socks["mem"].sent) == 1
    assert (len(socks["fol"].sent) == 1) is followers_hear
