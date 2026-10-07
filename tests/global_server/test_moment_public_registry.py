"""Tests for :class:`MomentPublicRegistry` (GFS broker)."""

from __future__ import annotations

import orjson
import pytest

from socialhome.global_server.moment_public_registry import MomentPublicRegistry
from socialhome.global_server.repositories import (
    SqliteGfsMomentFollowRepo,
    SqliteGfsUserRegistrationRepo,
)
from socialhome.global_server.ws_registry import GfsWebSocketRegistry


class _StubWs:
    """Minimal stand-in for ``aiohttp.web.WebSocketResponse``."""

    def __init__(self) -> None:
        self.closed = False
        self.sent: list[str] = []

    async def send_str(self, msg: str) -> None:
        self.sent.append(msg)


def _sent_field(ws: "_StubWs", key: str) -> list:
    """Decoded values of ``key`` across every frame a stub WS received.

    Parses each frame so assertions are robust to JSON formatting
    (compact vs spaced) — the registry serialises with orjson, which is
    always compact.
    """
    return [orjson.loads(s).get(key) for s in ws.sent]


@pytest.fixture
async def gfs_setup(gfs_db):
    # Seed two paired instances on the GFS — the author and the follower.
    await gfs_db.enqueue(
        "INSERT INTO client_instances("
        "instance_id, display_name, public_key, status) "
        "VALUES('inst-author','Author','aa'*32,'active')"
    )
    await gfs_db.enqueue(
        "INSERT INTO client_instances("
        "instance_id, display_name, public_key, status) "
        "VALUES('inst-follower','Follower','bb'*32,'active')"
    )
    ws_registry = GfsWebSocketRegistry()
    return {
        "users": SqliteGfsUserRegistrationRepo(gfs_db),
        "follows": SqliteGfsMomentFollowRepo(gfs_db),
        "ws": ws_registry,
    }


def _make_registry(gfs_setup) -> MomentPublicRegistry:
    return MomentPublicRegistry(
        gfs_setup["users"],
        gfs_setup["follows"],
        gfs_setup["ws"],
    )


async def test_register_user_then_directory_lists_them(gfs_setup):
    reg = _make_registry(gfs_setup)
    user = await reg.register_user(
        user_id="u-author",
        instance_id="inst-author",
        username="alice",
        display_name="Alice",
        home_instance_pk="aa" * 32,
        picture_url=None,
    )
    assert user.status == "active"
    listing = await reg.list_directory()
    assert [u.user_id for u in listing] == ["u-author"]
    got = await reg.get_registration("u-author")
    assert got is not None and got.username == "alice"


async def test_deregister_removes_row(gfs_setup):
    reg = _make_registry(gfs_setup)
    await reg.register_user(
        user_id="u-author",
        instance_id="inst-author",
        username="alice",
        display_name="Alice",
        home_instance_pk="aa" * 32,
    )
    assert await reg.deregister_user("u-author") is True
    assert await reg.deregister_user("u-author") is False  # idempotent


async def test_add_follow_pushes_follow_changed_to_author_ws(gfs_setup):
    reg = _make_registry(gfs_setup)
    await reg.register_user(
        user_id="u-author",
        instance_id="inst-author",
        username="alice",
        display_name="Alice",
        home_instance_pk="aa" * 32,
    )
    author_ws = _StubWs()
    await gfs_setup["ws"].register("inst-author", author_ws)  # type: ignore[arg-type]
    follow = await reg.add_follow(
        follower_user_id="u-follower",
        follower_instance_id="inst-follower",
        followed_user_id="u-author",
    )
    assert follow.followed_user_id == "u-author"
    # Author received the follow_changed frame.
    assert "follow_changed" in _sent_field(author_ws, "type")
    assert "add" in _sent_field(author_ws, "action")


async def test_add_follow_unknown_author_raises(gfs_setup):
    reg = _make_registry(gfs_setup)
    with pytest.raises(LookupError):
        await reg.add_follow(
            follower_user_id="u-follower",
            follower_instance_id="inst-follower",
            followed_user_id="u-ghost",
        )


async def test_remove_follow_pushes_follow_changed_remove(gfs_setup):
    reg = _make_registry(gfs_setup)
    await reg.register_user(
        user_id="u-author",
        instance_id="inst-author",
        username="alice",
        display_name="Alice",
        home_instance_pk="aa" * 32,
    )
    await reg.add_follow(
        follower_user_id="u-follower",
        follower_instance_id="inst-follower",
        followed_user_id="u-author",
    )
    author_ws = _StubWs()
    await gfs_setup["ws"].register("inst-author", author_ws)  # type: ignore[arg-type]
    ok = await reg.remove_follow(
        follower_user_id="u-follower", followed_user_id="u-author"
    )
    assert ok is True
    assert "remove" in _sent_field(author_ws, "action")
    # Idempotent on a second call.
    ok2 = await reg.remove_follow(
        follower_user_id="u-follower", followed_user_id="u-author"
    )
    assert ok2 is False


async def test_fan_out_moment_pushes_to_each_unique_follower_instance(gfs_setup):
    reg = _make_registry(gfs_setup)
    await reg.register_user(
        user_id="u-author",
        instance_id="inst-author",
        username="alice",
        display_name="Alice",
        home_instance_pk="aa" * 32,
    )
    await reg.add_follow(
        follower_user_id="u-follower",
        follower_instance_id="inst-follower",
        followed_user_id="u-author",
    )
    follower_ws = _StubWs()
    await gfs_setup["ws"].register("inst-follower", follower_ws)  # type: ignore[arg-type]
    delivered = await reg.fan_out_moment(
        envelope={
            "moment_id": "m-1",
            "author_user_id": "u-author",
            "content": "hi",
            "signature": "stub",
        }
    )
    assert delivered == 1
    assert "incoming_public_moment" in _sent_field(follower_ws, "type")


async def test_fan_out_moment_drops_author_with_no_followers(gfs_setup):
    reg = _make_registry(gfs_setup)
    await reg.register_user(
        user_id="u-author",
        instance_id="inst-author",
        username="alice",
        display_name="Alice",
        home_instance_pk="aa" * 32,
    )
    delivered = await reg.fan_out_moment(
        envelope={"moment_id": "m-1", "author_user_id": "u-author"}
    )
    assert delivered == 0


async def test_fan_out_moment_missing_author_logs_and_returns_zero(gfs_setup):
    reg = _make_registry(gfs_setup)
    delivered = await reg.fan_out_moment(envelope={"moment_id": "m-1"})
    assert delivered == 0


async def test_fan_out_delete_pushes_tombstone(gfs_setup):
    reg = _make_registry(gfs_setup)
    await reg.register_user(
        user_id="u-author",
        instance_id="inst-author",
        username="alice",
        display_name="Alice",
        home_instance_pk="aa" * 32,
    )
    await reg.add_follow(
        follower_user_id="u-follower",
        follower_instance_id="inst-follower",
        followed_user_id="u-author",
    )
    follower_ws = _StubWs()
    await gfs_setup["ws"].register("inst-follower", follower_ws)  # type: ignore[arg-type]
    delivered = await reg.fan_out_delete(
        envelope={"moment_id": "m-1", "author_user_id": "u-author"}
    )
    assert delivered == 1
    assert "incoming_public_moment_delete" in _sent_field(follower_ws, "type")


async def test_follower_count_reads_repo(gfs_setup):
    reg = _make_registry(gfs_setup)
    await reg.register_user(
        user_id="u-author",
        instance_id="inst-author",
        username="alice",
        display_name="Alice",
        home_instance_pk="aa" * 32,
    )
    assert await reg.follower_count("u-author") == 0
    await reg.add_follow(
        follower_user_id="u-follower",
        follower_instance_id="inst-follower",
        followed_user_id="u-author",
    )
    assert await reg.follower_count("u-author") == 1
