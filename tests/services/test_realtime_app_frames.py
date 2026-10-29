"""App frames — install / update / uninstall / turn on / turn off nudge open
Apps pages, the dashboard's Apps card and an open app tab, and never reach
someone the app's age gate keeps out."""

from __future__ import annotations

import json

import pytest

from socialhome.domain.events import (
    AppEnabledChanged,
    AppInstalled,
    AppUninstalled,
    AppUpdated,
)
from socialhome.domain.user import User
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.ws_manager import WebSocketManager
from socialhome.services.realtime_service import RealtimeService

_USERS = [
    User(user_id="adm", username="adm", display_name="A", is_admin=True),
    User(user_id="bob", username="bob", display_name="B"),
    User(
        user_id="kid",
        username="kid",
        display_name="K",
        child_protection_enabled=True,
        declared_age=9,
    ),
    User(
        user_id="teen",
        username="teen",
        display_name="T",
        child_protection_enabled=True,
        declared_age=16,
    ),
]


class _FakeUserRepo:
    async def list_active(self):
        return _USERS


class _FakeSpaceRepo:
    async def list_local_member_user_ids(self, space_id):
        return []


class _FakeWS:
    def __init__(self):
        self.sent: list[str] = []

    async def send_str(self, msg):
        self.sent.append(msg)

    @property
    def closed(self):
        return False


@pytest.fixture
async def env():
    bus = EventBus()
    ws = WebSocketManager()
    RealtimeService(
        bus, ws, user_repo=_FakeUserRepo(), space_repo=_FakeSpaceRepo()
    ).wire()
    socks = {u.user_id: _FakeWS() for u in _USERS}
    for uid, sock in socks.items():
        await ws.register(uid, sock)
    return bus, socks


def _frames(socks) -> dict[str, list[dict]]:
    return {uid: [json.loads(m) for m in s.sent] for uid, s in socks.items()}


@pytest.mark.parametrize(
    ("event", "frame_type"),
    [
        (
            AppInstalled(app_id="chess", name="Chess", installed_by="adm"),
            "app.installed",
        ),
        (
            AppUpdated(app_id="chess", name="Chess", old_version="1", new_version="2"),
            "app.updated",
        ),
        (AppUninstalled(app_id="chess"), "app.uninstalled"),
        (AppEnabledChanged(app_id="chess", enabled=True), "app.enabled"),
        (AppEnabledChanged(app_id="chess", enabled=False), "app.disabled"),
    ],
)
async def test_each_app_change_nudges_the_whole_household(env, event, frame_type):
    """No age gate: everyone gets a thin ``{type, app_id}`` frame."""
    bus, socks = env
    await bus.publish(event)
    for uid, frames in _frames(socks).items():
        assert frames == [{"type": frame_type, "app_id": "chess"}], uid


@pytest.mark.parametrize(
    "event",
    [
        AppInstalled(app_id="chess", name="Chess", installed_by="adm", min_age=13),
        AppUpdated(
            app_id="chess", name="Chess", old_version="1", new_version="2", min_age=13
        ),
        AppUninstalled(app_id="chess", min_age=13),
        AppEnabledChanged(app_id="chess", enabled=True, min_age=13),
        AppEnabledChanged(app_id="chess", enabled=False, min_age=13),
    ],
)
async def test_an_age_gated_app_never_reaches_who_it_keeps_out(env, event):
    """Same audience as ``GET /api/apps``: admins see every app; a protected
    account below ``min_age`` never learns of it, one above it does."""
    bus, socks = env
    await bus.publish(event)
    got = {uid for uid, frames in _frames(socks).items() if frames}
    assert got == {"adm", "bob", "teen"}
