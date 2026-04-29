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
        self.close_calls: list[tuple] = []

    async def send_str(self, msg: str) -> None:
        if self.fail:
            raise ConnectionResetError("boom")
        self.sent.append(msg)

    async def close(self, *, code: int = 1000, message: bytes = b"") -> None:
        self.close_calls.append((code, message))
        self.closed = True


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


# ─── Shutdown ────────────────────────────────────────────────────────────


async def test_close_all_sends_going_away_to_every_socket():
    """The shutdown path must close every socket so the WS handler's
    `async for msg in ws` loop unblocks. Without it, Ctrl-C hangs as
    long as any browser tab still has the SPA open."""
    from aiohttp import WSCloseCode

    mgr = WebSocketManager()
    a = _FakeWS()
    b = _FakeWS()
    c = _FakeWS()
    await mgr.register("alice", a)
    await mgr.register("alice", b)
    await mgr.register("bob", c)

    await mgr.close_all()

    for ws in (a, b, c):
        assert len(ws.close_calls) == 1
        code, message = ws.close_calls[0]
        assert code == WSCloseCode.GOING_AWAY
        assert message == b"server shutting down"


async def test_close_all_no_op_when_empty():
    mgr = WebSocketManager()
    # Just confirm no-op doesn't raise.
    await mgr.close_all()


async def test_close_all_swallows_close_failures():
    """A broken socket whose close() raises must not block the rest."""

    class _ExplodingWS(_FakeWS):
        async def close(self, *, code=1000, message=b""):  # type: ignore[override]
            raise ConnectionResetError("boom")

    mgr = WebSocketManager()
    bad = _ExplodingWS()
    good = _FakeWS()
    await mgr.register("alice", bad)
    await mgr.register("bob", good)
    # Should not raise.
    await mgr.close_all()
    assert good.close_calls  # the good socket still got the close frame
