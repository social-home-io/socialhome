"""Coverage fill for the aiolibdatachannel-native transport.

Exercises helpers and internal drain loops in
:mod:`social_home.federation.transport` that end-to-end tests don't
hit directly. Uses the fake aiolibdatachannel module installed by
``tests/conftest.py``.
"""

from __future__ import annotations

import asyncio

import aiolibdatachannel as rtc

from social_home.domain.federation import FederationEventType
from social_home.federation.transport import (
    _RtcPeer,
    _build_rtc_config,
)


# ─── _build_rtc_config ──────────────────────────────────────────────────


def test_build_rtc_config_empty():
    cfg = _build_rtc_config([])
    assert cfg.ice_servers == []


def test_build_rtc_config_stun_urls():
    cfg = _build_rtc_config(
        [{"urls": "stun:stun.l.google.com:19302"}],
    )
    assert len(cfg.ice_servers) == 1
    assert cfg.ice_servers[0].url == "stun:stun.l.google.com:19302"
    assert cfg.ice_servers[0].username is None


def test_build_rtc_config_multiple_urls_in_single_entry():
    cfg = _build_rtc_config(
        [{"urls": ["stun:a", "stun:b"]}],
    )
    assert [s.url for s in cfg.ice_servers] == ["stun:a", "stun:b"]


def test_build_rtc_config_turn_with_credentials():
    """TURN user/password ride as first-class IceServer fields."""
    cfg = _build_rtc_config(
        [
            {
                "urls": "turn:turn.example.net:3478",
                "username": "alice",
                "credential": "secret",
            }
        ],
    )
    s = cfg.ice_servers[0]
    assert s.url == "turn:turn.example.net:3478"
    assert s.username == "alice"
    assert s.credential == "secret"


def test_build_rtc_config_turn_without_credentials_stays_bare():
    cfg = _build_rtc_config([{"urls": "turn:host"}])
    assert cfg.ice_servers[0].url == "turn:host"
    assert cfg.ice_servers[0].username is None
    assert cfg.ice_servers[0].credential is None


def test_build_rtc_config_mix():
    cfg = _build_rtc_config(
        [
            {"urls": ["stun:s1", "stun:s2"]},
            {
                "urls": "turn:t1",
                "username": "u",
                "credential": "p",
            },
        ],
    )
    assert len(cfg.ice_servers) == 3
    assert cfg.ice_servers[0].url == "stun:s1"
    assert cfg.ice_servers[2].username == "u"


# ─── _RtcPeer handshake paths ───────────────────────────────────────────


async def _collect_signals():
    events: list = []

    async def signaling(event_type, payload):
        events.append((event_type, payload))

    return events, signaling


async def _noop_inbound(_data):
    return None


async def test_peer_start_offer_signals_offer_sdp():
    events, signaling = await _collect_signals()
    peer = _RtcPeer(
        instance_id="p",
        ice_servers=None,
        signaling=signaling,
        inbound=_noop_inbound,
    )
    await peer.start_offer()
    # The fake pc always yields the canned _STUB_SDP.
    offer_events = [
        e for e in events if e[0] is FederationEventType.FEDERATION_RTC_OFFER
    ]
    assert offer_events
    assert offer_events[0][1]["sdp_type"] == "offer"
    assert peer._expected_answer_from == "p"
    peer.close()


async def test_peer_accept_offer_signals_answer_sdp():
    events, signaling = await _collect_signals()
    peer = _RtcPeer(
        instance_id="p",
        ice_servers=[{"urls": "stun:x"}],
        signaling=signaling,
        inbound=_noop_inbound,
    )
    await peer.accept_offer(sdp="remote-sdp", from_instance="p")
    assert peer._expected_answer_from is None
    answer_events = [
        e for e in events if e[0] is FederationEventType.FEDERATION_RTC_ANSWER
    ]
    assert answer_events
    peer.close()


async def test_peer_apply_answer_mismatch_returns_false():
    """S-14: answer from the wrong peer is rejected."""
    events, signaling = await _collect_signals()
    peer = _RtcPeer(
        instance_id="p",
        ice_servers=None,
        signaling=signaling,
        inbound=_noop_inbound,
    )
    await peer.start_offer()
    ok = await peer.apply_answer(sdp="x", from_instance="imposter")
    assert ok is False
    # Still expecting the true peer's answer.
    assert peer._expected_answer_from == "p"
    peer.close()


async def test_peer_apply_answer_match_returns_true():
    events, signaling = await _collect_signals()
    peer = _RtcPeer(
        instance_id="p",
        ice_servers=None,
        signaling=signaling,
        inbound=_noop_inbound,
    )
    await peer.start_offer()
    ok = await peer.apply_answer(sdp="a", from_instance="p")
    assert ok is True
    assert peer._expected_answer_from is None
    peer.close()


async def test_peer_apply_answer_when_pc_missing_is_safe():
    """After ``close()`` the pc is None — apply_answer must not crash."""
    events, signaling = await _collect_signals()
    peer = _RtcPeer(
        instance_id="p",
        ice_servers=None,
        signaling=signaling,
        inbound=_noop_inbound,
    )
    # No start_offer called → no expected_answer_from set → no S-14 block.
    ok = await peer.apply_answer(sdp="x", from_instance="whoever")
    assert ok is True


async def test_peer_add_ice_candidate_empty_is_noop():
    events, signaling = await _collect_signals()
    peer = _RtcPeer(
        instance_id="p",
        ice_servers=None,
        signaling=signaling,
        inbound=_noop_inbound,
    )
    await peer.start_offer()
    # Should not raise when candidate is empty.
    await peer.add_ice_candidate(candidate="", sdp_mid="0")
    peer.close()


async def test_peer_add_ice_candidate_real():
    events, signaling = await _collect_signals()
    peer = _RtcPeer(
        instance_id="p",
        ice_servers=None,
        signaling=signaling,
        inbound=_noop_inbound,
    )
    await peer.start_offer()
    await peer.add_ice_candidate(
        candidate="candidate:1 udp 1 1.1.1.1 5000 typ host",
        sdp_mid="0",
    )
    peer.close()


async def test_peer_add_ice_candidate_no_pc_is_safe():
    events, signaling = await _collect_signals()
    peer = _RtcPeer(
        instance_id="p",
        ice_servers=None,
        signaling=signaling,
        inbound=_noop_inbound,
    )
    # No start_offer → _pc is None.
    await peer.add_ice_candidate(candidate="c", sdp_mid="0")


async def test_peer_send_on_closed_channel_returns_false():
    events, signaling = await _collect_signals()
    peer = _RtcPeer(
        instance_id="p",
        ice_servers=None,
        signaling=signaling,
        inbound=_noop_inbound,
    )
    # No channel wired.
    assert await peer.send({"x": 1}) is False


async def test_peer_send_when_ready_writes_frame():
    events, signaling = await _collect_signals()
    peer = _RtcPeer(
        instance_id="p",
        ice_servers=None,
        signaling=signaling,
        inbound=_noop_inbound,
    )

    class _ReadyChannel:
        def __init__(self) -> None:
            self.sent: list = []
            self.buffered_amount = 0

        async def send(self, data):
            self.sent.append(data)

    ch = _ReadyChannel()
    peer._channel = ch  # type: ignore[attr-defined]
    peer._open.set()

    ok = await peer.send({"k": "v"})
    assert ok is True
    assert ch.sent


async def test_peer_send_drops_frame_when_over_hwm():
    """Frames are dropped when buffered_amount ≥ HWM."""
    events, signaling = await _collect_signals()
    peer = _RtcPeer(
        instance_id="p",
        ice_servers=None,
        signaling=signaling,
        inbound=_noop_inbound,
    )

    class _SaturatedChannel:
        def __init__(self, buffered: int) -> None:
            self.sent: list = []
            self.buffered_amount = buffered

        async def send(self, data):
            self.sent.append(data)

    ch = _SaturatedChannel(buffered=peer._send_hwm + 1)
    peer._channel = ch  # type: ignore[attr-defined]
    peer._open.set()

    ok = await peer.send({"k": "v"})
    assert ok is False
    assert ch.sent == []  # send was skipped entirely


async def test_peer_send_returns_false_on_rtc_error():
    events, signaling = await _collect_signals()
    peer = _RtcPeer(
        instance_id="p",
        ice_servers=None,
        signaling=signaling,
        inbound=_noop_inbound,
    )

    class _RaisingChannel:
        def __init__(self) -> None:
            self.buffered_amount = 0

        async def send(self, data):
            raise rtc.RTCError("nope")

    peer._channel = _RaisingChannel()  # type: ignore[attr-defined]
    peer._open.set()
    assert await peer.send({"k": "v"}) is False


async def test_peer_close_cancels_tasks_and_clears_state():
    events, signaling = await _collect_signals()
    peer = _RtcPeer(
        instance_id="p",
        ice_servers=None,
        signaling=signaling,
        inbound=_noop_inbound,
    )
    await peer.start_offer()
    assert peer._pc is not None
    peer.close()
    assert peer._pc is None
    assert peer._channel is None
    assert peer._closed is True


async def test_peer_drain_channel_marks_open_then_closed():
    """A channel that opens and immediately closes should flip the
    peer's ``_open`` bit on, then off."""
    events, signaling = await _collect_signals()
    peer = _RtcPeer(
        instance_id="p",
        ice_servers=None,
        signaling=signaling,
        inbound=_noop_inbound,
    )

    class _ShortLivedChannel:
        def __init__(self) -> None:
            self.is_closed = False
            self.is_open = False
            self._close_ev = asyncio.Event()

        async def wait_open(self) -> None:
            self.is_open = True

        async def wait_closed(self) -> None:
            await self._close_ev.wait()

        def __aiter__(self):
            return self

        async def __anext__(self):
            # Yield immediately then stop.
            raise StopAsyncIteration

        def close(self) -> None:
            self.is_closed = True
            self._close_ev.set()

    ch = _ShortLivedChannel()
    task = asyncio.create_task(peer._drain_channel(ch))
    # Give the task a chance to run wait_open + finish iterating.
    await asyncio.sleep(0.01)
    assert peer._open.is_set() is True or peer._closed is True
    ch.close()
    await task
    assert peer._closed is True


async def test_peer_drain_channel_handles_wait_open_failure():
    """If wait_open raises an rtc error, the loop logs and returns cleanly."""
    events, signaling = await _collect_signals()
    peer = _RtcPeer(
        instance_id="p",
        ice_servers=None,
        signaling=signaling,
        inbound=_noop_inbound,
    )

    class _BadChannel:
        async def wait_open(self) -> None:
            raise rtc.RTCError("no DTLS")

    await peer._drain_channel(_BadChannel())
    # Peer never reached open state.
    assert peer._open.is_set() is False


async def test_peer_drain_channel_parses_inbound_messages():
    """Feed a dict through the drain loop and assert _inbound received it."""
    received: list = []

    async def _capture(data):
        received.append(data)

    peer = _RtcPeer(
        instance_id="p",
        ice_servers=None,
        signaling=lambda *a, **kw: None,  # unused here
        inbound=_capture,
    )

    class _OneMessageChannel:
        def __init__(self) -> None:
            self._sent = False

        async def wait_open(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._sent:
                raise StopAsyncIteration
            self._sent = True
            return b'{"hello": "world"}'

    await peer._drain_channel(_OneMessageChannel())
    assert received == [{"hello": "world"}]


async def test_peer_drain_channel_skips_malformed_frame():
    received: list = []

    async def _capture(data):
        received.append(data)

    peer = _RtcPeer(
        instance_id="p",
        ice_servers=None,
        signaling=lambda *a, **kw: None,
        inbound=_capture,
    )

    class _JunkChannel:
        def __init__(self) -> None:
            self._sent = False

        async def wait_open(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._sent:
                raise StopAsyncIteration
            self._sent = True
            return b"not-json"

    await peer._drain_channel(_JunkChannel())
    # Bad JSON was logged + skipped; no inbound delivery.
    assert received == []


async def test_peer_drain_ice_handles_rtc_errors():
    """rtc errors inside the ICE iterator are swallowed + logged."""
    events, signaling = await _collect_signals()
    peer = _RtcPeer(
        instance_id="p",
        ice_servers=None,
        signaling=signaling,
        inbound=_noop_inbound,
    )

    class _BadPc:
        def ice_candidates(self):
            async def _it():
                raise rtc.RTCError("gathering aborted")
                yield  # pragma: no cover — after raise

            return _it()

    peer._pc = _BadPc()  # type: ignore[attr-defined]
    await peer._drain_ice()  # must not raise


async def test_peer_drain_incoming_channel_ignores_wrong_label():
    """Only channels labelled ``fed-v1`` are consumed."""
    events, signaling = await _collect_signals()
    peer = _RtcPeer(
        instance_id="p",
        ice_servers=None,
        signaling=signaling,
        inbound=_noop_inbound,
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

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    good = _FakeCh("fed-v1")

    class _Pc:
        def incoming_data_channels(self):
            async def _gen():
                yield _FakeCh("other-label")  # skipped
                yield good  # accepted

            return _gen()

        def spawn_task(self, coro):
            return asyncio.create_task(coro)

    peer._pc = _Pc()  # type: ignore[attr-defined]
    await peer._drain_incoming_channel()
    assert peer._channel is good
