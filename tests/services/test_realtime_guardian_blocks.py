"""§CP.F2: frames about one side of a guardian block never reach the other.

``GET /api/friends`` and ``GET /api/presence`` leave out everyone a guardian
block separates from the viewer, either way; the live frames that would
refresh those views (status, profile, whereabouts, online state) are
withheld from the same people, so a WebSocket can't carry what the REST
view hides. Everyone else still gets them.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from socialhome.domain.events import (
    CpBlockAdded,
    CpBlockRemoved,
    PresenceUpdated,
    SpaceMemberProfileUpdated,
    UserCameOnline,
    UserProfileUpdated,
    UserStatusChanged,
    UserWentIdle,
    UserWentOffline,
)
from socialhome.domain.user import User, UserStatus
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.ws_manager import WebSocketManager
from socialhome.services.realtime_service import RealtimeService

_USERS = [
    User(
        user_id="kid",
        username="kid",
        display_name="K",
        bio="trains",
        picture_hash="0123456789abcdef",
    ),
    User(user_id="bob", username="bob", display_name="B", bio="hi"),
    User(user_id="carol", username="carol", display_name="C"),
]
#: bob is blocked for kid; ``rex`` (a remote person) is blocked for kid too.
_PAIRS = {("kid", "bob"), ("kid", "rex")}


class _FakeUserRepo:
    async def list_active(self):
        return _USERS

    async def get(self, username):
        return next((u for u in _USERS if u.username == username), None)

    async def get_by_user_id(self, user_id):
        return next((u for u in _USERS if u.user_id == user_id), None)


class _FakeSpaceRepo:
    async def list_local_member_user_ids(self, space_id):
        return [u.user_id for u in _USERS]


class _FakeCpRepo:
    async def list_guardians(self, minor_user_id):
        return []

    async def list_block_counterparts(self, user_id):
        return frozenset(
            {b for a, b in _PAIRS if a == user_id}
            | {a for a, b in _PAIRS if b == user_id}
        )


class _FakeWS:
    def __init__(self):
        self.sent: list[str] = []

    async def send_str(self, msg):
        self.sent.append(msg)

    @property
    def closed(self):
        return False


async def _env(cp_repo):
    bus = EventBus()
    ws = WebSocketManager()
    RealtimeService(
        bus,
        ws,
        user_repo=_FakeUserRepo(),
        space_repo=_FakeSpaceRepo(),
        cp_repo=cp_repo,
    ).wire()
    socks = {u.user_id: _FakeWS() for u in _USERS}
    for uid, sock in socks.items():
        await ws.register(uid, sock)
    return bus, socks


@pytest.fixture
async def env():
    return await _env(_FakeCpRepo())


def _got(socks, frame_type):
    return {
        uid
        for uid, s in socks.items()
        if any(json.loads(m).get("type") == frame_type for m in s.sent)
    }


_NOW = datetime.now(timezone.utc)


@pytest.mark.parametrize(
    ("event", "frame_type"),
    [
        (
            UserStatusChanged(user_id="kid", status=UserStatus(text="x")),
            "user.status_changed",
        ),
        (
            UserProfileUpdated(
                user_id="kid",
                username="kid",
                display_name="K",
                bio="about me",
                picture_hash=None,
            ),
            "user.profile_updated",
        ),
        (
            PresenceUpdated(username="kid", state="zone", zone_name="School"),
            "presence.updated",
        ),
        (
            PresenceUpdated(username="kid", state="home", user_id="kid"),
            "presence.updated",
        ),
        (
            SpaceMemberProfileUpdated(
                space_id="sp",
                user_id="kid",
                space_display_name="K",
                picture_hash="0123456789abcdef",
            ),
            "space.member.profile_updated",
        ),
        (UserCameOnline(user_id="kid"), "user.online"),
        (UserWentIdle(user_id="kid", last_active_at=_NOW), "user.idle"),
        (UserWentOffline(user_id="kid", last_seen_at=_NOW), "user.offline"),
    ],
)
async def test_protected_accounts_frames_skip_the_blocked_person(
    env, event, frame_type
):
    bus, socks = env
    await bus.publish(event)
    got = _got(socks, frame_type)
    assert "bob" not in got
    assert "carol" in got


async def test_blocked_persons_frames_skip_the_protected_account(env):
    bus, socks = env
    await bus.publish(UserStatusChanged(user_id="bob", status=UserStatus(text="y")))
    assert _got(socks, "user.status_changed") == {"bob", "carol"}


async def test_blocked_remote_persons_status_skips_the_protected_account(env):
    """The inbound handler re-publishes a peer's status on the local bus."""
    bus, socks = env
    await bus.publish(UserStatusChanged(user_id="rex", status=UserStatus(text="z")))
    assert _got(socks, "user.status_changed") == {"bob", "carol"}


async def test_unknown_presence_user_goes_to_everyone(env):
    bus, socks = env
    await bus.publish(PresenceUpdated(username="ghost", state="home"))
    assert _got(socks, "presence.updated") == {"kid", "bob", "carol"}


async def test_without_child_protection_everyone_gets_the_frame():
    bus, socks = await _env(None)
    await bus.publish(UserStatusChanged(user_id="kid", status=UserStatus(text="x")))
    assert _got(socks, "user.status_changed") == {"kid", "bob", "carol"}


def _profile_frames(socks, uid) -> list[dict]:
    return [
        f
        for f in (json.loads(m) for m in socks[uid].sent)
        if f.get("type") == "user.profile_updated"
    ]


async def test_block_tells_each_open_session_the_other_is_name_only(env):
    """An open SPA drops the other side's picture and bio at once, not on
    its next reload — and nobody else hears anything."""
    bus, socks = env
    await bus.publish(CpBlockAdded(minor_user_id="kid", blocked_user_id="bob"))
    (to_bob,) = _profile_frames(socks, "bob")
    assert to_bob["user_id"] == "kid"
    assert to_bob["display_name"] == "K"
    assert to_bob["bio"] is None and to_bob["picture_url"] is None
    (to_kid,) = _profile_frames(socks, "kid")
    assert to_kid["user_id"] == "bob" and to_kid["bio"] is None
    assert _profile_frames(socks, "carol") == []


async def test_lifting_the_block_restores_the_profile_in_open_sessions():
    bus, socks = await _env(_LiftedCpRepo())
    await bus.publish(CpBlockRemoved(minor_user_id="kid", blocked_user_id="bob"))
    (to_bob,) = _profile_frames(socks, "bob")
    assert to_bob["bio"] == "trains"
    assert to_bob["picture_url"].startswith("api/users/kid/picture?v=")


async def test_a_remote_blocked_person_gets_no_local_frame(env):
    bus, socks = env
    await bus.publish(CpBlockAdded(minor_user_id="kid", blocked_user_id="rex"))
    assert all(_profile_frames(socks, uid) == [] for uid in socks)


class _LiftedCpRepo(_FakeCpRepo):
    async def list_block_counterparts(self, user_id):
        return frozenset()
