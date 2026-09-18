"""Tests for ``GfsWebSocketRegistry`` (spec §24.12)."""

from __future__ import annotations

from typing import Any

import pytest

from socialhome.global_server.ws_registry import GfsWebSocketRegistry


# ── Fakes ──────────────────────────────────────────────────────────────────────


class _FakeWS:
    """Stand-in for :class:`aiohttp.web.WebSocketResponse`.

    The registry only calls ``send_str`` and ``close``; we record both so
    tests can assert on them, and a ``broken`` flag triggers the same
    eviction path the real socket would on a dropped TCP connection.
    """

    def __init__(self, *, broken: bool = False) -> None:
        self.sent: list[str] = []
        self.closed_with: tuple[int, bytes] | None = None
        self._broken = broken

    @property
    def closed(self) -> bool:
        return self.closed_with is not None

    async def send_str(self, msg: str) -> None:
        if self._broken:
            raise ConnectionResetError("simulated drop")
        self.sent.append(msg)

    async def close(self, *, code: int = 1000, message: bytes = b"") -> None:
        self.closed_with = (code, message)


@pytest.fixture
def registry() -> GfsWebSocketRegistry:
    return GfsWebSocketRegistry()


# ── register / unregister ──────────────────────────────────────────────────────


async def test_register_then_unregister_clears_entry(registry):
    ws = _FakeWS()
    await registry.register("inst-1", ws)
    assert registry.is_connected("inst-1") is True
    assert registry.connection_count() == 1
    assert registry.connected_instances() == {"inst-1"}

    await registry.unregister("inst-1", ws)
    assert registry.is_connected("inst-1") is False
    assert registry.connection_count() == 0


async def test_register_evicts_previous_socket(registry):
    """A second connect for the same instance closes the first with 4409."""
    first = _FakeWS()
    second = _FakeWS()
    await registry.register("inst-1", first)
    await registry.register("inst-1", second)

    assert first.closed_with == (4409, b"replaced")
    assert registry.is_connected("inst-1") is True
    assert registry.connection_count() == 1


async def test_unregister_ignores_stale_socket(registry):
    """Unregistering a socket that's already been replaced is a no-op."""
    first = _FakeWS()
    second = _FakeWS()
    await registry.register("inst-1", first)
    await registry.register("inst-1", second)
    # First's owner sees the close and tries to unregister itself —
    # but the registry now points at ``second``; nothing should change.
    await registry.unregister("inst-1", first)
    assert registry.is_connected("inst-1") is True


# ── send ───────────────────────────────────────────────────────────────────────


async def test_send_returns_false_when_no_socket(registry):
    delivered = await registry.send("nobody-home", {"type": "relay", "x": 1})
    assert delivered is False


async def test_send_pushes_json_payload(registry):
    ws = _FakeWS()
    await registry.register("inst-1", ws)

    delivered = await registry.send("inst-1", {"type": "relay", "x": 1})
    assert delivered is True
    assert len(ws.sent) == 1
    assert '"type": "relay"' in ws.sent[0] or '"type":"relay"' in ws.sent[0]


async def test_send_drops_dead_socket_on_failure(registry):
    ws = _FakeWS(broken=True)
    await registry.register("inst-1", ws)

    delivered = await registry.send("inst-1", {"type": "relay"})
    assert delivered is False
    # Dead socket is evicted so a second call returns False without retrying.
    assert registry.is_connected("inst-1") is False


async def test_send_serializes_default_str_for_unknown_types(registry):
    """Datetime-like objects survive json.dumps via the ``default=str`` fallback."""

    class _NotJsonNative:
        def __str__(self) -> str:
            return "stringified"

    ws = _FakeWS()
    await registry.register("inst-1", ws)
    payload: dict[str, Any] = {"type": "relay", "obj": _NotJsonNative()}

    delivered = await registry.send("inst-1", payload)
    assert delivered is True
    assert "stringified" in ws.sent[0]


async def test_send_unserialisable_frame_returns_false_without_raising(registry):
    """An encode failure must not escape ``send``. Fan-out gathers every
    delivery, so a raised TypeError propagated out of ``_deliver_one`` and
    turned the whole relay request into a 500."""

    class _Unhashable:
        """A key orjson cannot encode — ``default=`` covers values, not keys."""

    ws = _FakeWS()
    await registry.register("inst-1", ws)
    payload: dict[Any, Any] = {"type": "relay", _Unhashable(): 1}

    delivered = await registry.send("inst-1", payload)
    assert delivered is False
    assert ws.sent == []
    # The socket itself is healthy — an encode bug must not evict it.
    assert registry.is_connected("inst-1") is True


async def test_send_unserialisable_frame_does_not_break_broadcast(registry):
    """One unserialisable frame returns 0 delivered rather than raising."""
    ws = _FakeWS()
    await registry.register("inst-1", ws)

    class _Unhashable:
        pass

    assert await registry.broadcast({_Unhashable(): 1}) == 0


# ── broadcast ────────────────────────────────────────────────────────────────────


async def test_broadcast_sends_to_every_connected_socket(registry):
    a, b, c = _FakeWS(), _FakeWS(), _FakeWS()
    await registry.register("inst-1", a)
    await registry.register("inst-2", b)
    await registry.register("inst-3", c)

    delivered = await registry.broadcast(
        {"type": "server_info_updated", "server_name": "New"}
    )

    assert delivered == 3
    for ws in (a, b, c):
        assert len(ws.sent) == 1
        assert "server_info_updated" in ws.sent[0]


async def test_broadcast_tolerates_failed_socket(registry):
    """A broken socket is evicted and not counted; the rest still receive."""
    good = _FakeWS()
    broken = _FakeWS(broken=True)
    await registry.register("inst-good", good)
    await registry.register("inst-broken", broken)

    delivered = await registry.broadcast(
        {"type": "server_info_updated", "server_name": "X"}
    )

    assert delivered == 1
    assert len(good.sent) == 1
    assert registry.is_connected("inst-broken") is False


async def test_broadcast_returns_zero_when_no_sockets(registry):
    delivered = await registry.broadcast({"type": "server_info_updated"})
    assert delivered == 0


# ── close_all ──────────────────────────────────────────────────────────────────


async def test_close_all_closes_every_socket(registry):
    a, b = _FakeWS(), _FakeWS()
    await registry.register("inst-1", a)
    await registry.register("inst-2", b)

    await registry.close_all()

    assert a.closed_with == (1001, b"server-shutdown")
    assert b.closed_with == (1001, b"server-shutdown")
    assert registry.connection_count() == 0
