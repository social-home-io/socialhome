"""Tests for ``GfsWebSocketRegistry`` (spec §24.12)."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any

import orjson
import pytest

from socialhome.crypto import b64url_encode
from socialhome.domain.ws_binary_frame import decode_binary_frame
from socialhome.global_server import ws_registry as ws_registry_mod
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
        self.sent_bytes = []
        self.closed_with: tuple[int, bytes] | None = None
        self._broken = broken

    @property
    def closed(self) -> bool:
        return self.closed_with is not None

    async def send_str(self, msg: str) -> None:
        if self._broken:
            raise ConnectionResetError("simulated drop")
        self.sent.append(msg)

    async def send_bytes(self, data: bytes) -> None:
        if self._broken:
            raise ConnectionResetError("simulated drop")
        self.sent_bytes.append(data)

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


# ── stalled sockets ────────────────────────────────────────────────────────────


class _StalledWS(_FakeWS):
    """A peer that stopped reading: ``send_str`` blocks on a paused
    transport until something cancels it."""

    def __init__(self) -> None:
        super().__init__()
        self.send_started = asyncio.Event()

    async def send_str(self, msg: str) -> None:
        self.send_started.set()
        await asyncio.Event().wait()


async def test_a_stalled_send_times_out_returns_false_and_evicts(registry, monkeypatch):
    """A household that stops reading must not hold the sender (an envelope
    accept, a drain, a fan-out worker) until the heartbeat notices."""
    monkeypatch.setattr(ws_registry_mod, "WS_SEND_TIMEOUT_S", 0.02)
    ws = _StalledWS()
    await registry.register("inst-1", ws)

    assert await asyncio.wait_for(registry.send("inst-1", {"a": 1}), timeout=1) is False
    assert registry.is_connected("inst-1") is False
    # The stalled socket is closed in the background so the household
    # reconnects (and drains its queue) instead of hanging on.
    await registry.close_all()
    assert ws.closed_with is not None
    # The next send fails fast — no second stall.
    assert (
        await asyncio.wait_for(registry.send("inst-1", {"a": 1}), timeout=0.01) is False
    )


def test_send_timeout_is_the_documented_value():
    assert ws_registry_mod.WS_SEND_TIMEOUT_S == 10.0


# ── fan-out counters ───────────────────────────────────────────────────────────


async def test_a_delivered_frame_is_counted_by_kind_and_bytes(registry):
    ws = _FakeWS()
    await registry.register("inst-1", ws)
    frame = {"type": "relay", "event_type": "space_item", "payload": "é" * 100}
    assert await registry.send("inst-1", frame) is True
    snap = registry.fanout_stats.snapshot()
    assert snap["by_kind"] == {
        "relay:space_item": {"frames": 1, "bytes": len(ws.sent[0].encode())}
    }


async def test_an_undelivered_frame_is_not_counted(registry):
    await registry.register("inst-1", _FakeWS(broken=True))
    assert await registry.send("inst-1", {"type": "relay"}) is False
    assert await registry.send("nobody", {"type": "relay"}) is False
    assert registry.fanout_stats.snapshot()["total"] == {"frames": 0, "bytes": 0}


# ── ws_binary1 (binary frames, per connection) ─────────────────────────────

_CT = f"{b64url_encode(os.urandom(12))}:{b64url_encode(os.urandom(1040))}"
_ITEM = {
    "type": "relay",
    "space_id": "sp",
    "event_type": "space_item",
    "epoch": 3,
    "payload": _CT,
}


async def test_a_binary_socket_gets_a_binary_frame_and_a_text_one_text(registry):
    """Per connection: the same frame goes binary to the socket that asked
    and as text to the other — nobody falls back."""
    binary, text = _FakeWS(), _FakeWS()
    await registry.register("bin", binary, binary=True)
    await registry.register("txt", text)
    assert await registry.send("bin", _ITEM)
    assert await registry.send("txt", _ITEM)
    assert binary.sent == [] and len(binary.sent_bytes) == 1
    assert text.sent_bytes == [] and len(text.sent) == 1
    assert decode_binary_frame(binary.sent_bytes[0]) == orjson.loads(text.sent[0])
    assert len(binary.sent_bytes[0]) < len(text.sent[0].encode()) * 0.8
    by_kind = registry.fanout_stats.snapshot()["by_kind"]["relay:space_item"]
    assert by_kind["bytes"] == len(binary.sent_bytes[0]) + len(text.sent[0])


async def test_a_frame_without_a_listed_ciphertext_stays_text(registry):
    ws = _FakeWS()
    await registry.register("bin", ws, binary=True)
    assert await registry.send("bin", {"type": "new_subscriber", "space_id": "sp"})
    assert len(ws.sent) == 1 and ws.sent_bytes == []


async def test_the_binary_header_encodes_exactly_like_a_text_frame(registry):
    """Same encoder for both (``default=str``, datetimes passed through it),
    so a binary follower reads the same values a text follower does."""
    binary, text = _FakeWS(), _FakeWS()
    await registry.register("bin", binary, binary=True)
    await registry.register("txt", text)
    frame = {**_ITEM, "when": datetime(2026, 1, 1, tzinfo=UTC)}
    assert await registry.send("bin", frame)
    assert await registry.send("txt", frame)
    assert decode_binary_frame(binary.sent_bytes[0]) == orjson.loads(text.sent[0])


async def test_the_binary_choice_follows_the_current_socket(registry):
    first, second = _FakeWS(), _FakeWS()
    await registry.register("inst", first, binary=True)
    await registry.register("inst", second)  # reconnect without the label
    assert await registry.send("inst", _ITEM)
    assert len(second.sent) == 1 and second.sent_bytes == []
    await registry.register("inst", first, binary=True)
    await registry.unregister("inst", first)
    assert "inst" not in registry._binary


async def test_a_dead_binary_socket_is_evicted(registry):
    await registry.register("bin", _FakeWS(broken=True), binary=True)
    assert await registry.send("bin", _ITEM) is False
    assert registry.is_connected("bin") is False
    assert "bin" not in registry._binary


# ── space_item_no_cert (no plaintext writer cert, per connection) ─────────

_CERT = {
    "cert_suite": "ed25519",
    "space_id": "sp",
    "epoch": 3,
    "instance_pk": "A" * 43,
    "scope": "write",
    "issued_at": 1_790_000_000,
    "cert_sig": "B" * 86,
}
_TRUSTED_ITEM = {**_ITEM, "writer_cert": _CERT}


async def test_a_socket_that_asked_gets_member_items_without_the_frame_cert(
    registry,
):
    """The follower verifies the cert inside the ciphertext; the plaintext
    v1 copy is the server's authorization material, 279 B of every frame.
    The socket that listed ``space_item_no_cert`` no longer gets it; the
    other one still does."""
    lean, full = _FakeWS(), _FakeWS()
    await registry.register("lean", lean, no_frame_cert=True)
    await registry.register("full", full)
    assert await registry.send("lean", _TRUSTED_ITEM)
    assert await registry.send("full", _TRUSTED_ITEM)
    assert orjson.loads(lean.sent[0]) == _ITEM
    assert orjson.loads(full.sent[0]) == _TRUSTED_ITEM
    assert "writer_cert" in _TRUSTED_ITEM  # the shared frame is untouched


async def test_a_binary_lean_socket_gets_the_binary_frame_without_the_cert(registry):
    ws = _FakeWS()
    await registry.register("both", ws, binary=True, no_frame_cert=True)
    assert await registry.send("both", _TRUSTED_ITEM)
    assert decode_binary_frame(ws.sent_bytes[0]) == _ITEM


async def test_only_member_items_lose_the_cert(registry):
    """Another frame kind keeps every field, whatever it is named."""
    ws = _FakeWS()
    await registry.register("lean", ws, no_frame_cert=True)
    other = {"type": "relay", "event_type": "space_post_public", "writer_cert": 1}
    assert await registry.send("lean", other)
    assert orjson.loads(ws.sent[0]) == other


async def test_the_cert_choice_follows_the_current_socket(registry):
    first, second = _FakeWS(), _FakeWS()
    await registry.register("inst", first, no_frame_cert=True)
    await registry.register("inst", second)  # reconnect without the label
    assert await registry.send("inst", _TRUSTED_ITEM)
    assert orjson.loads(second.sent[0]) == _TRUSTED_ITEM
    await registry.register("inst", first, no_frame_cert=True)
    await registry.unregister("inst", first)
    assert "inst" not in registry._no_frame_cert
    await registry.register("gone", _FakeWS(broken=True), no_frame_cert=True)
    assert await registry.send("gone", _TRUSTED_ITEM) is False
    assert "gone" not in registry._no_frame_cert
    await registry.register("all", _FakeWS(), no_frame_cert=True)
    await registry.close_all()
    assert registry._no_frame_cert == set()
