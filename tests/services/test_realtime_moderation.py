"""Audience of the ``space.moderation.*`` frames in RealtimeService.

The frame carries the full pending item (post body, rejection reason),
so it goes only to the people who can read the moderation queue — the
space's owner, admins and moderators — plus the submitter for the outcome of their
own item.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone

import pytest

from socialhome.domain.events import (
    SpaceJoinDenied,
    SpaceJoinRequested,
    SpaceModerationApproved,
    SpaceModerationExpired,
    SpaceModerationQueued,
    SpaceModerationRejected,
)
from socialhome.domain.space import SpaceMember, SpaceModerationItem, SpaceRole
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.ws_manager import WebSocketManager
from socialhome.services.realtime_service import RealtimeService


class _FakeUserRepo:
    async def list_active(self):
        return []


def _member(uid: str, role: SpaceRole) -> SpaceMember:
    return SpaceMember(space_id="sp-1", user_id=uid, role=role, joined_at="2026")


class _FakeSpaceRepo:
    def __init__(self, members):
        self._members = members

    async def list_members(self, space_id):
        return self._members.get(space_id, [])

    async def list_local_member_user_ids(self, space_id):
        return [m.user_id for m in self._members.get(space_id, [])]


class _FakeWS:
    def __init__(self):
        self.sent: list[str] = []

    async def send_str(self, msg):
        self.sent.append(msg)

    @property
    def closed(self):
        return False


def _item(submitted_by: str = "sub") -> SpaceModerationItem:
    now = datetime(2026, 4, 15, tzinfo=timezone.utc)
    return SpaceModerationItem(
        id="mod-1",
        space_id="sp-1",
        feature="post",
        action="create",
        submitted_by=submitted_by,
        payload={"id": "p1", "content": "secret body"},
        current_snapshot=None,
        submitted_at=now,
        expires_at=now,
        rejection_reason="off topic",
    )


_UIDS = ("own", "adm", "mod", "sub", "mem", "fol")


@pytest.fixture
async def env():
    bus = EventBus()
    ws = WebSocketManager()
    svc = RealtimeService(
        bus,
        ws,
        user_repo=_FakeUserRepo(),
        space_repo=_FakeSpaceRepo(
            {
                "sp-1": [
                    _member("own", SpaceRole.OWNER),
                    _member("adm", SpaceRole.ADMIN),
                    _member("mod", SpaceRole.MODERATOR),
                    _member("sub", SpaceRole.MEMBER),
                    _member("mem", SpaceRole.MEMBER),
                    _member("fol", SpaceRole.SUBSCRIBER),
                ]
            }
        ),
    )
    svc.wire()
    socks = {}
    for uid in _UIDS:
        socks[uid] = _FakeWS()
        await ws.register(uid, socks[uid])
    return bus, socks


def _got(sock, frame_type):
    return any(frame_type in m for m in sock.sent)


async def test_moderation_queued_reaches_owner_and_admins_only(env):
    bus, socks = env
    await bus.publish(SpaceModerationQueued(item=_item()))
    assert _got(socks["own"], "space.moderation.queued")
    assert _got(socks["adm"], "space.moderation.queued")
    # A moderator works the queue (v_41).
    assert _got(socks["mod"], "space.moderation.queued")
    # Plain members and subscribers never get the pending item over WS.
    for uid in ("mem", "fol"):
        assert socks[uid].sent == [], uid
    # The submitter, who already knows what they submitted, gets only the
    # content-free receipt that refreshes their pending strip.
    assert not _got(socks["sub"], "space.moderation.queued")
    [receipt] = [json.loads(m) for m in socks["sub"].sent]
    assert receipt == {
        "type": "space.moderation.mine",
        "space_id": "sp-1",
        "item_id": "mod-1",
        "feature": "post",
        "action": "create",
        "status": "pending",
    }
    assert "secret body" not in socks["sub"].sent[0]


@pytest.mark.parametrize(
    ("event_cls", "frame_type"),
    [
        (SpaceModerationApproved, "space.moderation.approved"),
        (SpaceModerationRejected, "space.moderation.rejected"),
    ],
)
async def test_moderation_outcome_reaches_admins_and_submitter(
    env, event_cls, frame_type
):
    bus, socks = env
    await bus.publish(event_cls(item=_item()))
    for uid in ("own", "adm", "mod", "sub"):
        assert _got(socks[uid], frame_type), uid
    for uid in ("mem", "fol"):
        assert socks[uid].sent == [], uid


async def test_moderation_outcome_not_duplicated_for_admin_submitter(env):
    bus, socks = env
    await bus.publish(SpaceModerationApproved(item=_item(submitted_by="adm")))
    approved = [m for m in socks["adm"].sent if "space.moderation.approved" in m]
    assert len(approved) == 1


async def test_moderation_expired_reaches_moderators_and_submitter(env):
    bus, socks = env
    await bus.publish(SpaceModerationExpired(item=_item()))
    for uid in ("own", "adm", "mod", "sub"):
        assert _got(socks[uid], "space.moderation.expired"), uid
    assert _got(socks["sub"], "space.moderation.mine")
    for uid in ("mem", "fol"):
        assert socks[uid].sent == [], uid


async def test_moderation_outcome_skips_submitter_not_in_space(env):
    """A submitter who has since left the space gets nothing either —
    the outcome is theirs, but only while they still belong to it."""
    bus, socks = env
    item = dataclasses.replace(_item(), submitted_by="gone")
    await bus.publish(SpaceModerationRejected(item=item))
    assert _got(socks["own"], "space.moderation.rejected")
    for uid in ("sub", "mem", "fol"):
        assert socks[uid].sent == [], uid


async def test_join_requested_reaches_owner_and_admins_only(env):
    bus, socks = env
    await bus.publish(SpaceJoinRequested(space_id="sp-1", user_id="x", request_id="r"))
    assert _got(socks["own"], "space.join.requested")
    assert _got(socks["adm"], "space.join.requested")
    # Join requests are settings authority — a moderator is not told.
    for uid in ("mod", "sub", "mem", "fol"):
        assert socks[uid].sent == [], uid


async def test_join_denied_reaches_admins_and_requester(env):
    bus, socks = env
    await bus.publish(
        SpaceJoinDenied(space_id="sp-1", user_id="mem", request_id="r", denied_by="own")
    )
    for uid in ("own", "adm", "mem"):
        assert _got(socks[uid], "space.join.denied"), uid
    for uid in ("mod", "sub", "fol"):
        assert socks[uid].sent == [], uid
