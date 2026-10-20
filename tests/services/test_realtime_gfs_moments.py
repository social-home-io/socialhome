"""A stranger's moment that came through a GFS never reaches a protected
account's sockets (§CP.R) — not even the ``moment.created`` refresh hint.
A household moment still reaches everyone."""

from __future__ import annotations

import pytest

from socialhome.domain.events import MomentCreated
from socialhome.domain.user import User
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.ws_manager import WebSocketManager
from socialhome.services.realtime_service import RealtimeService

_USERS = [
    User(user_id="adult", username="adult", display_name="A"),
    User(
        user_id="kid", username="kid", display_name="K", child_protection_enabled=True
    ),
]


class _Users:
    async def list_active(self):
        return _USERS


class _Spaces:
    async def list_local_member_user_ids(self, space_id):
        return []


class _Sock:
    def __init__(self) -> None:
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
    RealtimeService(bus, ws, user_repo=_Users(), space_repo=_Spaces()).wire()
    socks = {u.user_id: _Sock() for u in _USERS}
    for uid, sock in socks.items():
        await ws.register(uid, sock)
    return bus, socks


def _event(received_via):
    return MomentCreated(
        moment_id="m1",
        author_user_id="u-stranger",
        content="x",
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=None,
        parent_author_user_id=None,
        origin_instance_id="inst-x",
        expires_at="2099-01-01T00:00:00+00:00",
        received_via=received_via,
    )


def _got(socks) -> set[str]:
    return {
        uid for uid, s in socks.items() if any("moment.created" in m for m in s.sent)
    }


async def test_a_gfs_moment_frame_skips_protected_accounts(env):
    bus, socks = env
    await bus.publish(_event("gfs"))
    assert _got(socks) == {"adult"}


async def test_a_household_moment_frame_reaches_everyone(env):
    bus, socks = env
    await bus.publish(_event(None))
    assert _got(socks) == {"adult", "kid"}
