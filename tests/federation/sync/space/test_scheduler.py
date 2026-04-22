"""Unit tests for :class:`SpaceSyncScheduler`."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from socialhome.domain.events import PairingConfirmed
from socialhome.domain.federation import FederationEventType, PairingStatus
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceType,
)
from socialhome.federation.sync.space.scheduler import SpaceSyncScheduler
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.reconnect_queue import ReconnectSyncQueue


class _FakeFederation:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_event(self, *, to_instance_id, event_type, payload, space_id=None):
        self.sent.append(
            {
                "to": to_instance_id,
                "type": event_type,
                "payload": payload,
                "space_id": space_id,
            }
        )
        return SimpleNamespace(ok=True)


class _FakeFedRepo:
    def __init__(self, instances):
        self._instances = instances

    async def list_instances(self, *, source=None, status=None):
        return self._instances


class _FakeSpaceRepo:
    def __init__(self, *, spaces_by_type, members_by_space):
        self._spaces = spaces_by_type
        self._members = members_by_space

    async def list_by_type(self, space_type):
        return self._spaces.get(space_type, [])

    async def list_member_instances(self, space_id):
        return self._members.get(space_id, [])


def _space(space_id: str) -> Space:
    return Space(
        id=space_id,
        name=space_id,
        owner_instance_id="self",
        owner_username="admin",
        identity_public_key="aa" * 32,
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.HOUSEHOLD,
        join_mode=JoinMode.INVITE_ONLY,
    )


def _peer(instance_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=instance_id,
        status=PairingStatus.CONFIRMED,
    )


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def queue():
    return ReconnectSyncQueue(concurrency=2)


async def test_on_pairing_confirmed_enqueues_for_shared_spaces(bus, queue):
    fed = _FakeFederation()
    spaces = {SpaceType.HOUSEHOLD: [_space("sp-1"), _space("sp-2")]}
    # The new peer is only a member of sp-1.
    members = {"sp-1": ["peer-a"], "sp-2": ["other-peer"]}
    sched = SpaceSyncScheduler(
        bus=bus,
        federation=fed,
        federation_repo=_FakeFedRepo([_peer("peer-a")]),
        space_repo=_FakeSpaceRepo(
            spaces_by_type=spaces,
            members_by_space=members,
        ),
        queue=queue,
        own_instance_id="self",
    )
    sched.wire()
    await queue.start()
    try:
        await bus.publish(PairingConfirmed(instance_id="peer-a"))
        # Let the worker drain.
        await asyncio.sleep(0.1)
    finally:
        await queue.stop()
    # Expect one SPACE_SYNC_BEGIN sent for sp-1 to peer-a (not sp-2).
    beg = [s for s in fed.sent if s["type"] == FederationEventType.SPACE_SYNC_BEGIN]
    assert len(beg) == 1
    assert beg[0]["to"] == "peer-a"
    assert beg[0]["space_id"] == "sp-1"


async def test_on_pairing_confirmed_ignores_self(bus, queue):
    fed = _FakeFederation()
    sched = SpaceSyncScheduler(
        bus=bus,
        federation=fed,
        federation_repo=_FakeFedRepo([]),
        space_repo=_FakeSpaceRepo(
            spaces_by_type={SpaceType.HOUSEHOLD: [_space("sp-1")]},
            members_by_space={"sp-1": ["self"]},
        ),
        queue=queue,
        own_instance_id="self",
    )
    sched.wire()
    await queue.start()
    try:
        await bus.publish(PairingConfirmed(instance_id="self"))
        await asyncio.sleep(0.05)
    finally:
        await queue.stop()
    assert fed.sent == []


async def test_enqueue_sync_for_space_sends_begin(bus, queue):
    fed = _FakeFederation()
    sched = SpaceSyncScheduler(
        bus=bus,
        federation=fed,
        federation_repo=_FakeFedRepo([]),
        space_repo=_FakeSpaceRepo(
            spaces_by_type={},
            members_by_space={},
        ),
        queue=queue,
        own_instance_id="self",
    )
    await queue.start()
    try:
        await sched.enqueue_sync_for_space(
            space_id="sp-1",
            peer_instance_id="peer-a",
        )
        await asyncio.sleep(0.05)
    finally:
        await queue.stop()
    assert len(fed.sent) == 1
    assert fed.sent[0]["type"] == FederationEventType.SPACE_SYNC_BEGIN
    assert fed.sent[0]["payload"]["space_id"] == "sp-1"
    assert fed.sent[0]["payload"]["sync_mode"] == "initial"
    assert fed.sent[0]["payload"]["prefer_direct"] is True


async def test_periodic_tick_enqueues_for_every_confirmed_peer(bus, queue):
    fed = _FakeFederation()
    # Two confirmed peers, one shared space.
    spaces = {SpaceType.HOUSEHOLD: [_space("sp-1")]}
    members = {"sp-1": ["peer-a", "peer-b"]}
    sched = SpaceSyncScheduler(
        bus=bus,
        federation=fed,
        federation_repo=_FakeFedRepo([_peer("peer-a"), _peer("peer-b")]),
        space_repo=_FakeSpaceRepo(
            spaces_by_type=spaces,
            members_by_space=members,
        ),
        queue=queue,
        own_instance_id="self",
    )
    await queue.start()
    try:
        await sched._tick_once()
        await asyncio.sleep(0.05)
    finally:
        await queue.stop()
    # One SPACE_SYNC_BEGIN per peer.
    beg = [s for s in fed.sent if s["type"] == FederationEventType.SPACE_SYNC_BEGIN]
    assert {s["to"] for s in beg} == {"peer-a", "peer-b"}


async def test_start_stop_idempotent(bus, queue):
    sched = SpaceSyncScheduler(
        bus=bus,
        federation=_FakeFederation(),
        federation_repo=_FakeFedRepo([]),
        space_repo=_FakeSpaceRepo(spaces_by_type={}, members_by_space={}),
        queue=queue,
        own_instance_id="self",
    )
    await sched.start()
    await sched.start()  # second call is a no-op
    await sched.stop()
    await sched.stop()  # second call is a no-op
