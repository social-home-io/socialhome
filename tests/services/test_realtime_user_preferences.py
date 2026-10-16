"""Tests for user-preferences WS broadcasts in RealtimeService.

Exercises :meth:`RealtimeService._on_user_preferences_changed` — the
bridge from the in-process bus to the ``user.preferences_changed`` WS
frame that the SPA uses to refresh personalised surfaces without a page
reload. The frame is unicast to the owner's WS sessions only.
"""

from __future__ import annotations

import pytest

from socialhome.domain.events import UserPreferencesChanged
from socialhome.domain.user import User
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.realtime_service import RealtimeService

pytestmark = pytest.mark.asyncio


# ─── Fakes ───────────────────────────────────────────────────────────────────


class _CapturingWs:
    """Stand-in for :class:`WebSocketManager` — records every broadcast."""

    __slots__ = ("calls",)

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []

    async def broadcast_to_users(self, user_ids, payload: dict) -> int:
        self.calls.append((list(user_ids), payload))
        return len(list(user_ids))


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


# ─── UserPreferencesChanged ──────────────────────────────────────────────────


async def test_user_preferences_changed_broadcasts_only_to_owner(env):
    """UserPreferencesChanged → user.preferences_changed frame to user_id only."""
    _svc, bus, ws = env
    await bus.publish(
        UserPreferencesChanged(user_id="u-1", changed={"hide_highlights": True})
    )
    assert len(ws.calls) == 1
    recipients, frame = ws.calls[0]
    assert recipients == ["u-1"]
    assert frame["type"] == "user.preferences_changed"
    assert frame["user_id"] == "u-1"
    assert frame["changed"] == {"hide_highlights": True}


async def test_user_preferences_changed_payload_shape(env):
    """Frame keys are exactly {'type', 'user_id', 'changed'}."""
    _svc, bus, ws = env
    await bus.publish(UserPreferencesChanged(user_id="u-2", changed={"theme": "dark"}))
    assert len(ws.calls) == 1
    _recipients, frame = ws.calls[0]
    assert set(frame.keys()) == {"type", "user_id", "changed"}


async def test_user_preferences_changed_does_not_broadcast_to_other_users(env):
    """Recipient list contains only the event owner — not the full household."""
    _svc, bus, ws = env
    await bus.publish(
        UserPreferencesChanged(user_id="u-1", changed={"hide_bazaar": False})
    )
    recipients, _frame = ws.calls[0]
    # Must be a single-element list with ONLY the owner's id
    assert recipients == ["u-1"]
    assert "u-2" not in recipients


async def test_user_preferences_changed_multiple_keys(env):
    """changed dict with several keys is passed through intact."""
    _svc, bus, ws = env
    payload = {"hide_highlights": True, "theme": "dark", "language": "de"}
    await bus.publish(UserPreferencesChanged(user_id="u-1", changed=payload))
    _recipients, frame = ws.calls[0]
    assert frame["changed"] == payload
