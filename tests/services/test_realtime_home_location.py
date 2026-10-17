"""Tests for home-location WS broadcasts in RealtimeService.

Exercises :meth:`RealtimeService._on_local_home_location_updated` and
:meth:`RealtimeService._on_peer_home_changed` — the bridge from the
in-process bus to the ``local.home_changed`` / ``peer.home_changed`` WS
frames that drive the federation map's live-update pins.
"""

from __future__ import annotations

import pytest

from socialhome.domain.events import LocalHomeLocationUpdated, PeerHomeChanged
from socialhome.domain.user import User
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.realtime_service import RealtimeService

pytestmark = pytest.mark.asyncio


# ─── Fakes ───────────────────────────────────────────────────────────────────


class _CapturingWs:
    """Stand-in for :class:`WebSocketManager` — records every broadcast."""

    __slots__ = ("calls",)

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def broadcast_to_users(self, user_ids, payload: dict) -> int:
        self.calls.append(payload)
        return len(list(user_ids))

    async def broadcast_to_user(
        self, user_id: str, payload: dict
    ) -> int:  # pragma: no cover
        self.calls.append(payload)
        return 1


class _FakeUserRepo:
    """Returns two stub users so ``_broadcast_household`` has recipients."""

    def __init__(self) -> None:
        self._users = [
            User(user_id="u-1", username="alice", display_name="Alice"),
            User(user_id="u-2", username="bob", display_name="Bob"),
        ]

    async def list_active(self):
        return self._users


# ─── Fixture ─────────────────────────────────────────────────────────────────


@pytest.fixture
def env():
    """RealtimeService wired to capturing stubs."""
    bus = EventBus()
    ws = _CapturingWs()
    svc = RealtimeService(
        bus=bus,
        ws=ws,
        user_repo=_FakeUserRepo(),
        space_repo=object(),
    )
    svc.wire()
    return svc, bus, ws


# ─── LocalHomeLocationUpdated ────────────────────────────────────────────────


async def test_local_home_location_updated_broadcasts_local_frame(env):
    """LocalHomeLocationUpdated → ``local.home_changed`` frame to all clients."""
    _svc, bus, ws = env
    await bus.publish(LocalHomeLocationUpdated(latitude=52.52, longitude=13.405))
    assert len(ws.calls) == 1
    frame = ws.calls[0]
    assert frame["type"] == "local.home_changed"
    assert frame["latitude"] == 52.52
    assert frame["longitude"] == 13.405


async def test_local_home_location_updated_frame_has_no_extra_keys(env):
    """Frame carries exactly type + latitude + longitude (no noise)."""
    _svc, bus, ws = env
    await bus.publish(LocalHomeLocationUpdated(latitude=51.0, longitude=0.0))
    frame = ws.calls[0]
    assert set(frame.keys()) == {"type", "latitude", "longitude"}


# ─── PeerHomeChanged ─────────────────────────────────────────────────────────


async def test_peer_home_changed_broadcasts_peer_frame(env):
    """PeerHomeChanged → ``peer.home_changed`` frame with instance_id + coords."""
    _svc, bus, ws = env
    await bus.publish(
        PeerHomeChanged(
            instance_id="peer-1",
            latitude=53.55,
            longitude=9.99,
        )
    )
    assert len(ws.calls) == 1
    frame = ws.calls[0]
    assert frame["type"] == "peer.home_changed"
    assert frame["instance_id"] == "peer-1"
    assert frame["latitude"] == 53.55
    assert frame["longitude"] == 9.99


async def test_peer_home_changed_frame_has_no_extra_keys(env):
    """Frame carries exactly type + instance_id + latitude + longitude."""
    _svc, bus, ws = env
    await bus.publish(
        PeerHomeChanged(
            instance_id="peer-2",
            latitude=48.8566,
            longitude=2.3522,
        )
    )
    frame = ws.calls[0]
    assert set(frame.keys()) == {"type", "instance_id", "latitude", "longitude"}


async def test_peer_home_changed_null_coords_pass_through(env):
    """PeerHomeChanged with None coords passes None through to the WS frame (no 0.0 substitution)."""
    _svc, bus, ws = env
    await bus.publish(
        PeerHomeChanged(
            instance_id="peer-revoke",
            latitude=None,
            longitude=None,
        )
    )
    assert len(ws.calls) == 1
    frame = ws.calls[0]
    assert frame["type"] == "peer.home_changed"
    assert frame["instance_id"] == "peer-revoke"
    assert frame["latitude"] is None
    assert frame["longitude"] is None
