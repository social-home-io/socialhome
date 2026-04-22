"""Tests for WebSocketManager (in-process fan-out)."""

from __future__ import annotations

import json


from socialhome.infrastructure.ws_manager import WebSocketManager


class _FakeWS:
    """Minimal stand-in for aiohttp.web.WebSocketResponse."""

    def __init__(self, *, fail: bool = False, closed: bool = False):
        self.fail = fail
        self.closed = closed
        self.sent: list[str] = []

    async def send_str(self, msg: str) -> None:
        if self.fail:
            raise ConnectionResetError("boom")
        self.sent.append(msg)


# ─── Registry ────────────────────────────────────────────────────────────


async def test_register_increments_count():
    mgr = WebSocketManager()
    ws = _FakeWS()
    await mgr.register("alice", ws)
    assert mgr.connection_count() == 1
    assert mgr.session_count_for_user("alice") == 1
    assert "alice" in mgr.connected_users()


async def test_register_is_idempotent():
    mgr = WebSocketManager()
    ws = _FakeWS()
    await mgr.register("alice", ws)
    await mgr.register("alice", ws)
    assert mgr.session_count_for_user("alice") == 1


async def test_unregister_removes_and_cleans_user_when_empty():
    mgr = WebSocketManager()
    ws = _FakeWS()
    await mgr.register("alice", ws)
    await mgr.unregister("alice", ws)
    assert mgr.connection_count() == 0
    assert "alice" not in mgr.connected_users()


# ─── Fan-out ──────────────────────────────────────────────────────────────


async def test_broadcast_to_user_delivers_to_all_sessions():
    mgr = WebSocketManager()
    a, b = _FakeWS(), _FakeWS()
    await mgr.register("alice", a)
    await mgr.register("alice", b)
    delivered = await mgr.broadcast_to_user("alice", {"type": "hi"})
    assert delivered == 2
    assert json.loads(a.sent[0]) == {"type": "hi"}
    assert json.loads(b.sent[0]) == {"type": "hi"}


async def test_broadcast_to_user_unknown_user_is_zero():
    mgr = WebSocketManager()
    delivered = await mgr.broadcast_to_user("nobody", {"x": 1})
    assert delivered == 0


async def test_broadcast_drops_dead_socket_silently():
    mgr = WebSocketManager()
    good, bad = _FakeWS(), _FakeWS(fail=True)
    await mgr.register("alice", good)
    await mgr.register("alice", bad)
    delivered = await mgr.broadcast_to_user("alice", {"type": "x"})
    assert delivered == 1
    # Dead socket has been pruned.
    assert mgr.session_count_for_user("alice") == 1


async def test_broadcast_drops_closed_socket_without_sending():
    mgr = WebSocketManager()
    closed = _FakeWS(closed=True)
    await mgr.register("alice", closed)
    delivered = await mgr.broadcast_to_user("alice", {"type": "x"})
    assert delivered == 0
    assert closed.sent == []


async def test_broadcast_to_users_aggregates_count():
    mgr = WebSocketManager()
    await mgr.register("alice", _FakeWS())
    await mgr.register("bob", _FakeWS())
    delivered = await mgr.broadcast_to_users(["alice", "bob", "missing"], {"x": 1})
    assert delivered == 2


async def test_broadcast_all_hits_every_user():
    mgr = WebSocketManager()
    await mgr.register("alice", _FakeWS())
    await mgr.register("bob", _FakeWS())
    await mgr.register("carol", _FakeWS())
    delivered = await mgr.broadcast_all({"type": "x"})
    assert delivered == 3


async def test_broadcast_to_users_handles_empty_list():
    mgr = WebSocketManager()
    assert await mgr.broadcast_to_users([], {}) == 0
