"""§CP frames name a protected account — admins, the account and its
guardians only, never the rest of the household."""

from __future__ import annotations

import json

import pytest

from socialhome.domain.events import (
    CpBlockAdded,
    CpBlockRemoved,
    CpGuardianAdded,
    CpGuardianRemoved,
    CpProtectionDisabled,
    CpProtectionEnabled,
)
from socialhome.domain.user import User
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.ws_manager import WebSocketManager
from socialhome.services.realtime_service import RealtimeService

_USERS = [
    User(user_id="adm", username="adm", display_name="A", is_admin=True),
    User(user_id="kid", username="kid", display_name="K"),
    User(user_id="mom", username="mom", display_name="M"),
    User(user_id="bob", username="bob", display_name="B"),
]


class _FakeUserRepo:
    async def list_active(self):
        return _USERS

    async def get(self, username):
        return next((u for u in _USERS if u.username == username), None)


class _FakeSpaceRepo:
    async def list_local_member_user_ids(self, space_id):
        return []


class _FakeCpRepo:
    async def list_guardians(self, minor_user_id):
        return ["mom"] if minor_user_id == "kid" else []


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
        bus,
        ws,
        user_repo=_FakeUserRepo(),
        space_repo=_FakeSpaceRepo(),
        cp_repo=_FakeCpRepo(),
    ).wire()
    socks = {u.user_id: _FakeWS() for u in _USERS}
    for uid, sock in socks.items():
        await ws.register(uid, sock)
    return bus, socks


def _got(socks, frame_type):
    return {uid for uid, s in socks.items() if any(frame_type in m for m in s.sent)}


async def test_protection_enabled_skips_the_rest_of_the_household(env):
    bus, socks = env
    await bus.publish(CpProtectionEnabled(minor_username="kid", declared_age=12))
    assert _got(socks, "cp.protection_enabled") == {"adm", "kid", "mom"}
    # The recorded age never rides the frame.
    assert not any("declared_age" in m for s in socks.values() for m in s.sent)


async def test_protection_disabled_same_audience(env):
    bus, socks = env
    await bus.publish(CpProtectionDisabled(minor_username="kid"))
    assert _got(socks, "cp.protection_disabled") == {"adm", "kid", "mom"}


async def test_guardian_frames_reach_the_named_guardian(env):
    bus, socks = env
    await bus.publish(CpGuardianAdded(minor_user_id="kid", guardian_user_id="bob"))
    assert _got(socks, "cp.guardian_added") == {"adm", "kid", "mom", "bob"}
    await bus.publish(CpGuardianRemoved(minor_user_id="kid", guardian_user_id="bob"))
    assert "bob" in _got(socks, "cp.guardian_removed")


async def test_block_frames_skip_unrelated_members(env):
    bus, socks = env
    await bus.publish(CpBlockAdded(minor_user_id="kid", blocked_user_id="x"))
    assert _got(socks, "cp.block_added") == {"adm", "kid", "mom"}


async def test_without_cp_repo_admins_and_account_still_hear(env):
    bus = EventBus()
    ws = WebSocketManager()
    RealtimeService(
        bus, ws, user_repo=_FakeUserRepo(), space_repo=_FakeSpaceRepo()
    ).wire()
    socks = {u.user_id: _FakeWS() for u in _USERS}
    for uid, sock in socks.items():
        await ws.register(uid, sock)
    await bus.publish(CpProtectionEnabled(minor_username="kid", declared_age=12))
    assert _got(socks, "cp.protection_enabled") == {"adm", "kid"}


@pytest.mark.parametrize(
    "event",
    [
        CpProtectionEnabled(minor_username="kid", declared_age=12),
        CpProtectionDisabled(minor_username="kid"),
        CpGuardianAdded(minor_user_id="kid", guardian_user_id="mom"),
        CpGuardianRemoved(minor_user_id="kid", guardian_user_id="mom"),
        CpBlockAdded(minor_user_id="kid", blocked_user_id="bob"),
        CpBlockRemoved(minor_user_id="kid", blocked_user_id="bob"),
    ],
)
async def test_the_account_alone_is_told_to_reload_its_protection(env, event):
    """``me.protection_changed`` reaches only the account itself, with no
    data — its SPA refetches ``/api/me`` + ``/api/me/protection``."""
    bus, socks = env
    await bus.publish(event)
    assert _got(socks, "me.protection_changed") == {"kid"}
    frames = [m for m in socks["kid"].sent if "me.protection_changed" in m]
    assert frames
    assert all(json.loads(m) == {"type": "me.protection_changed"} for m in frames)
