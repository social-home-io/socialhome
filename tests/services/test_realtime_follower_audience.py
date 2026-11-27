"""Every space frame reaches every seat, a follower's included (P10, owner
2026-11-26: a follower reads everything a member does — it replaces owner
decision 13): approval proposals, zones and the sync status like the
``GET …/proposals`` / ``…/zones`` reads they keep live, and the roster
frames (joined / left / profile, D3, v_96)."""

from __future__ import annotations

import orjson
import pytest

from socialhome.domain.events import (
    SpaceMemberJoined,
    SpaceMemberLeft,
    SpaceMemberProfileUpdated,
    SpaceProposalUpdated,
    SpaceSyncStateChanged,
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
    # A follower syncs like a member, so it gets the sync status too.
    SpaceSyncStateChanged(
        space_id="sp-1",
        sync={"state": "behind", "last_ok_at": None, "waiting_to_send": 0},
    ),
]


@pytest.mark.parametrize("event", _EVENTS, ids=lambda e: type(e).__name__)
async def test_people_frames_reach_followers_too(env, event):
    bus, socks = env
    await bus.publish(event)
    for uid in _ROLES:
        assert len(socks[uid].sent) == 1, uid


# ─── Roster frames: everyone sees the same roster (D3, v_96) ───────────────

_ROSTER_FRAMES = [
    SpaceMemberJoined(space_id="sp-1", user_id="fol2", role=SpaceRole.SUBSCRIBER),
    SpaceMemberLeft(space_id="sp-1", user_id="fol2", role=SpaceRole.SUBSCRIBER),
    SpaceMemberLeft(space_id="sp-1", user_id="gone"),
    SpaceMemberProfileUpdated(
        space_id="sp-1", user_id="fol2", space_display_name="F", picture_hash=None
    ),
    SpaceMemberJoined(space_id="sp-1", user_id="mem", role=SpaceRole.MEMBER),
    SpaceMemberLeft(space_id="sp-1", user_id="gone", role=SpaceRole.MEMBER),
    SpaceMemberProfileUpdated(
        space_id="sp-1", user_id="mem", space_display_name="M", picture_hash=None
    ),
    # A seat on another household: same frame, same audience.
    SpaceMemberProfileUpdated(
        space_id="sp-1",
        user_id="u-remote",
        space_display_name="R",
        picture_hash=None,
        origin_instance_id="peer-1",
    ),
]


@pytest.mark.parametrize(
    "event", _ROSTER_FRAMES, ids=lambda e: type(e).__name__ + ":" + e.user_id
)
async def test_roster_frames_reach_every_seat(env, event):
    """A frame about a follower reaches the followers too, and one about a
    member reaches them as before — a follower's roster lists everyone."""
    bus, socks = env
    await bus.publish(event)
    for uid in _ROLES:
        assert len(socks[uid].sent) == 1, uid


async def test_the_sync_state_frame_carries_the_status(env):
    bus, socks = env
    sync = {
        "state": "host_offline",
        "last_ok_at": "2026-11-21T10:00:00+00:00",
        "waiting_to_send": 2,
    }
    await bus.publish(SpaceSyncStateChanged(space_id="sp-1", sync=sync))
    frame = orjson.loads(socks["mem"].sent[0])
    assert frame == {"type": "space.sync_state", "space_id": "sp-1", "sync": sync}
