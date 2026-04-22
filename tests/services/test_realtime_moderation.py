"""Coverage for SpaceModerationApproved/Rejected paths in RealtimeService."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.domain.events import (
    SpaceModerationApproved,
    SpaceModerationQueued,
    SpaceModerationRejected,
)
from socialhome.domain.space import SpaceModerationItem
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.ws_manager import WebSocketManager
from socialhome.services.realtime_service import RealtimeService


class _FakeUserRepo:
    async def list_active(self):
        return []


class _FakeSpaceRepo:
    def __init__(self, members):
        self._members = members

    async def list_local_member_user_ids(self, space_id):
        return self._members.get(space_id, [])


class _FakeWS:
    def __init__(self):
        self.sent: list[str] = []

    async def send_str(self, msg):
        self.sent.append(msg)

    @property
    def closed(self):
        return False


def _item():
    now = datetime(2026, 4, 15, tzinfo=timezone.utc)
    return SpaceModerationItem(
        id="mod-1",
        space_id="sp-1",
        feature="post",
        action="create",
        submitted_by="a-id",
        payload={"id": "p1", "content": "x"},
        current_snapshot=None,
        submitted_at=now,
        expires_at=now,
    )


@pytest.fixture
async def env():
    bus = EventBus()
    ws = WebSocketManager()
    svc = RealtimeService(
        bus,
        ws,
        user_repo=_FakeUserRepo(),
        space_repo=_FakeSpaceRepo({"sp-1": ["alice", "bob"]}),
    )
    svc.wire()
    return bus, ws


async def test_moderation_queued_fans_to_space(env):
    bus, ws = env
    sock = _FakeWS()
    await ws.register("alice", sock)
    await bus.publish(SpaceModerationQueued(item=_item()))
    assert any("space.moderation.queued" in m for m in sock.sent)


async def test_moderation_approved_fans_to_space(env):
    bus, ws = env
    sock = _FakeWS()
    await ws.register("alice", sock)
    await bus.publish(SpaceModerationApproved(item=_item()))
    assert any("space.moderation.approved" in m for m in sock.sent)


async def test_moderation_rejected_fans_to_space(env):
    bus, ws = env
    sock = _FakeWS()
    await ws.register("alice", sock)
    await bus.publish(SpaceModerationRejected(item=_item()))
    assert any("space.moderation.rejected" in m for m in sock.sent)
