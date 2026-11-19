"""Coverage fill for :class:`SyncRtcSession` — drain loops + config helper."""

from __future__ import annotations

import asyncio

import aiolibdatachannel as rtc
import pytest

from socialhome.federation.sync_rtc import (
    CHANNEL_LABEL,
    SEND_HWM_BYTES,
    SyncRtcSession,
    _build_rtc_config,
)

# ─── _build_rtc_config ──────────────────────────────────────────────────


def test_build_rtc_config_turn_with_creds():
    cfg = _build_rtc_config(
        [
            {
                "urls": "turn:relay.example.net:5349",
                "username": "bob",
                "credential": "pw",
            }
        ],
    )
    s = cfg.ice_servers[0]
    assert s.url == "turn:relay.example.net:5349"
    assert s.username == "bob"
    assert s.credential == "pw"


def test_build_rtc_config_empty_is_ok():
    cfg = _build_rtc_config([])
    assert cfg.ice_servers == []


def test_build_rtc_config_accepts_list_urls():
    cfg = _build_rtc_config([{"urls": ["stun:a", "stun:b"]}])
    assert [s.url for s in cfg.ice_servers] == ["stun:a", "stun:b"]


# ─── close() lifecycle ──────────────────────────────────────────────────


async def test_close_clears_pc_and_channel():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp",
        requester_instance_id="r",
        provider_instance_id="p",
    )
    await s.create_offer()  # wires a channel on the fake pc
    s.close()
    assert s._pc is None
    assert s._channel is None
    assert s._closed is True


async def test_close_twice_is_idempotent():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp",
        requester_instance_id="r",
        provider_instance_id="p",
    )
    s.close()
    s.close()  # must not raise


# ─── is_ready / is_closed property branches ─────────────────────────────


async def test_is_ready_false_before_open():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp",
        requester_instance_id="r",
        provider_instance_id="p",
    )
    assert s.is_ready is False
    assert s.is_closed is False


async def test_is_closed_true_after_close():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp",
        requester_instance_id="r",
        provider_instance_id="p",
    )
    s.close()
    assert s.is_closed is True


# ─── send_chunk happy path ──────────────────────────────────────────────


async def test_send_chunk_writes_to_channel():
    """When the channel is wired, send_chunk forwards bytes via dc.send."""
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp",
        requester_instance_id="r",
        provider_instance_id="p",
        role="provider",
    )
    await s.create_offer()  # creates fake channel

    class _FakeCh:
        def __init__(self) -> None:
            self.sent: list = []
            self.buffered_amount = 0

        async def send(self, data):
            self.sent.append(data)

    ch = _FakeCh()
    s._channel = ch  # type: ignore[attr-defined]
    await s.send_chunk(b"hello")
    assert ch.sent == [b"hello"]


async def test_send_chunk_raises_when_over_hwm():
    """Backpressured channel → ConnectionError so sync falls back to relay."""
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp",
        requester_instance_id="r",
        provider_instance_id="p",
        role="provider",
    )
    await s.create_offer()

    class _FullCh:
        def __init__(self) -> None:
            self.sent: list = []
            self.buffered_amount = SEND_HWM_BYTES + 1

        async def send(self, data):
            self.sent.append(data)

    ch = _FullCh()
    s._channel = ch  # type: ignore[attr-defined]
    with pytest.raises(ConnectionError):
        await s.send_chunk(b"hello")
    assert ch.sent == []


# ─── _watch_channel / _watch_incoming drain loops ───────────────────────


async def test_watch_channel_marks_ready_then_closed():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp",
        requester_instance_id="r",
        provider_instance_id="p",
    )

    class _FakeCh:
        def __init__(self) -> None:
            self._closed = asyncio.Event()

        async def wait_open(self) -> None:
            return None

        async def wait_closed(self) -> None:
            await self._closed.wait()

        def close(self) -> None:
            self._closed.set()

    ch = _FakeCh()
    task = asyncio.create_task(s._watch_channel(ch))
    await asyncio.sleep(0.01)
    assert s._ready.is_set() is True
    ch.close()
    await task
    assert s._closed is True


async def test_watch_channel_handles_wait_open_failure():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp",
        requester_instance_id="r",
        provider_instance_id="p",
    )

    class _BadCh:
        async def wait_open(self) -> None:
            raise rtc.RTCError("handshake failed")

    await s._watch_channel(_BadCh())
    # Should never have flipped ready.
    assert s._ready.is_set() is False


async def test_watch_incoming_ignores_wrong_label():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp",
        requester_instance_id="r",
        provider_instance_id="p",
        role="requester",
    )

    class _FakeCh:
        def __init__(self, label):
            self.label = label

        def set_buffered_amount_low_threshold(self, n):
            self._hwm = n

        async def wait_open(self):
            return None

        async def wait_closed(self):
            return None

    good = _FakeCh(CHANNEL_LABEL)

    class _FakePc:
        def incoming_data_channels(self):
            async def _gen():
                yield _FakeCh("wrong")
                yield good

            return _gen()

        def spawn_task(self, coro):
            return asyncio.create_task(coro)

    s._pc = _FakePc()  # type: ignore[attr-defined]
    await s._watch_incoming()
    assert s._channel is good


# ─── send_chunk paces on the library's drain event (aiolibdatachannel ≥ 2026.11.10) ─


class _FixedDrainChannel:
    """Drain bookkeeping of aiolibdatachannel 2026.11.10
    (social-home-io/aiolibdatachannel#25): a send clears the drain event
    only when the buffer is ABOVE the low threshold, and the event is set
    again once the buffer drains to the threshold. Before that release a
    send cleared it whenever any bytes stayed buffered, so a stream of
    small chunks parked the next send forever (#1347)."""

    def __init__(self, *, buffered: int, threshold: int) -> None:
        self.sent: list = []
        self.buffered_amount = buffered
        self._threshold = threshold
        self._drained = asyncio.Event()
        self._drained.set()

    async def send(self, data, *, wait_for_drain: bool = True) -> None:
        if wait_for_drain:
            await self._drained.wait()
        self.sent.append(data)
        if self.buffered_amount > self._threshold:
            self._drained.clear()

    def drain_to(self, amount: int) -> None:
        self.buffered_amount = amount
        if amount <= self._threshold:
            self._drained.set()


async def _provider_session() -> SyncRtcSession:
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp",
        requester_instance_id="r",
        provider_instance_id="p",
        role="provider",
    )
    await s.create_offer()
    return s


async def test_send_chunk_stream_of_small_chunks_never_waits():
    """A few KiB stay buffered after every chunk — under the channel's low
    threshold (half the HWM), so no chunk of a space sync stream waits."""
    s = await _provider_session()
    ch = _FixedDrainChannel(buffered=4096, threshold=SEND_HWM_BYTES // 2)
    s._channel = ch  # type: ignore[attr-defined]
    for i in range(5):
        await asyncio.wait_for(s.send_chunk(f"c{i}".encode()), timeout=1.0)
    assert ch.sent == [b"c0", b"c1", b"c2", b"c3", b"c4"]


async def test_send_chunk_waits_for_the_drain_above_the_low_threshold():
    """Over the low threshold (still under the HWM) the next chunk waits for
    the library's drain event and goes out once the requester catches up —
    the backpressure a large stream relies on."""
    s = await _provider_session()
    ch = _FixedDrainChannel(
        buffered=SEND_HWM_BYTES // 2 + 1, threshold=SEND_HWM_BYTES // 2
    )
    s._channel = ch  # type: ignore[attr-defined]
    await s.send_chunk(b"first")
    second = asyncio.create_task(s.send_chunk(b"second"))
    await asyncio.sleep(0.02)
    assert not second.done()
    ch.drain_to(0)
    await asyncio.wait_for(second, timeout=1.0)
    assert ch.sent == [b"first", b"second"]
