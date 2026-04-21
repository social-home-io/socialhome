"""Unit tests for :class:`DmHistoryScheduler`."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from social_home.domain.events import ConnectionReachable, PairingConfirmed
from social_home.domain.federation import FederationEventType
from social_home.federation.sync.dm_history.scheduler import (
    RATE_LIMIT_SECONDS,
    DmHistoryScheduler,
)
from social_home.infrastructure.event_bus import EventBus
from social_home.infrastructure.reconnect_queue import ReconnectSyncQueue


class _FakeFederation:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_event(self, *, to_instance_id, event_type, payload, space_id=None):
        self.sent.append(
            {
                "to": to_instance_id,
                "type": event_type,
                "payload": payload,
            }
        )


class _FakeConvRepo:
    def __init__(self, convs_by_instance, latest_by_conv=None):
        self._convs = convs_by_instance
        self._latest = latest_by_conv or {}

    async def list_conversations_with_remote_member(self, instance_id):
        return list(self._convs.get(instance_id, []))

    async def list_messages(self, conversation_id, *, before=None, limit=50):
        msg = self._latest.get(conversation_id)
        return [msg] if msg else []


class _Msg:
    def __init__(self, iso: str):
        self.created_at = datetime.fromisoformat(iso)


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
async def queue():
    q = ReconnectSyncQueue(concurrency=2)
    yield q
    await q.stop()


async def test_pairing_confirmed_enqueues_history_requests(bus, queue):
    fed = _FakeFederation()
    repo = _FakeConvRepo({"peer-a": ["conv-1", "conv-2"]})
    sched = DmHistoryScheduler(
        bus=bus,
        federation=fed,
        conversation_repo=repo,
        queue=queue,
        own_instance_id="self",
    )
    sched.wire()
    await queue.start()
    await bus.publish(PairingConfirmed(instance_id="peer-a"))
    await asyncio.sleep(0.1)
    reqs = [s for s in fed.sent if s["type"] == FederationEventType.DM_HISTORY_REQUEST]
    assert {s["payload"]["conversation_id"] for s in reqs} == {"conv-1", "conv-2"}


async def test_connection_reachable_enqueues_history_requests(bus, queue):
    fed = _FakeFederation()
    latest = {"conv-1": _Msg("2026-04-01T00:00:00+00:00")}
    repo = _FakeConvRepo({"peer-b": ["conv-1"]}, latest_by_conv=latest)
    sched = DmHistoryScheduler(
        bus=bus,
        federation=fed,
        conversation_repo=repo,
        queue=queue,
        own_instance_id="self",
    )
    sched.wire()
    await queue.start()
    await bus.publish(ConnectionReachable(instance_id="peer-b"))
    await asyncio.sleep(0.1)
    reqs = [s for s in fed.sent if s["type"] == FederationEventType.DM_HISTORY_REQUEST]
    assert len(reqs) == 1
    assert reqs[0]["payload"]["since"] == "2026-04-01T00:00:00+00:00"


async def test_ignores_own_instance(bus, queue):
    fed = _FakeFederation()
    repo = _FakeConvRepo({"self": ["conv-x"]})
    sched = DmHistoryScheduler(
        bus=bus,
        federation=fed,
        conversation_repo=repo,
        queue=queue,
        own_instance_id="self",
    )
    sched.wire()
    await queue.start()
    await bus.publish(PairingConfirmed(instance_id="self"))
    await asyncio.sleep(0.05)
    assert fed.sent == []


async def test_rate_limit_blocks_rapid_repeats(bus, queue):
    fed = _FakeFederation()
    repo = _FakeConvRepo({"peer-a": ["conv-1"]})
    sched = DmHistoryScheduler(
        bus=bus,
        federation=fed,
        conversation_repo=repo,
        queue=queue,
        own_instance_id="self",
    )
    await queue.start()
    assert await sched._enqueue_for_peer("peer-a") == 1
    # Second call within the window → rate-limited to zero.
    assert await sched._enqueue_for_peer("peer-a") == 0


async def test_rate_limit_expires_after_window(bus, queue):
    fed = _FakeFederation()
    repo = _FakeConvRepo({"peer-a": ["conv-1"]})
    sched = DmHistoryScheduler(
        bus=bus,
        federation=fed,
        conversation_repo=repo,
        queue=queue,
        own_instance_id="self",
    )
    await queue.start()
    await sched._enqueue_for_peer("peer-a")
    # Backdate the recorded timestamp beyond the window.
    sched._last_request_at[("peer-a", "conv-1")] -= RATE_LIMIT_SECONDS + 1
    assert await sched._enqueue_for_peer("peer-a") == 1
