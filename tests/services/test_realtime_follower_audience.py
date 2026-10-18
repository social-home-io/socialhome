"""Frames about a space's people never reach its followers (owner decision
13): approval proposals and zones go to the members only, like the
``GET …/proposals`` / ``…/zones`` reads they keep live."""

from __future__ import annotations

import pytest

from socialhome.domain.events import (
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
