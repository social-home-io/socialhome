"""Tests for FederationTransport (§24.12.5).

The test conftest injects a fake ``aiolibdatachannel`` module into
``sys.modules`` before any production imports, so the DataChannel
state machine uses deterministic fake objects. The peer ``is_ready``
flag is flipped explicitly by marking ``_open`` / ``_closed`` on
``_RtcPeer``. That's enough to exercise the facade's primary /
fallback branches without the native binding.
"""

from __future__ import annotations

import asyncio
import time

import aiolibdatachannel as rtc

from socialhome.domain.events import PeerTransportChanged
from socialhome.domain.federation import (
    DeliveryResult,
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation import transport as transport_mod
from socialhome.federation.transport import (
    MAX_HANDSHAKE_RESTARTS,
    RTC_RETRY_BACKOFF_BASE_S,
    RTC_RETRY_BACKOFF_FACTOR,
    RTC_RETRY_BACKOFF_MAX_S,
    RTC_RETRY_JITTER,
    FederationTransport,
    HttpsInboxTransport,
    _build_rtc_config,
    _ice_fingerprint,
    _RtcPeer,
)
from socialhome.infrastructure.event_bus import EventBus


# ─── Fakes ────────────────────────────────────────────────────────────────


def _fake_instance(iid: str = "peer-1") -> RemoteInstance:
    return RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url="https://peer/wh",
        local_inbox_id=f"wh-{iid}",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )


class _RecordingHttpsInbox:
    """Drop-in replacement for :class:`HttpsInboxTransport` used by facade tests."""

    def __init__(self, *, ok: bool = True, status: int | None = 200) -> None:
        self.ok = ok
        self.status = status
        self.calls: list[tuple[RemoteInstance, dict]] = []

    async def send(self, *, instance, envelope_dict):
        self.calls.append((instance, envelope_dict))
        return self.ok, self.status


class _FakeSignaler:
    """Captures :meth:`FederationTransport.send` signalling round-trips."""

    def __init__(self):
        self.events: list[tuple[str, FederationEventType, dict]] = []

    async def __call__(self, to_instance_id, event_type, payload):
        self.events.append((to_instance_id, event_type, payload))
        return DeliveryResult(
            instance_id=to_instance_id,
            ok=True,
            status_code=200,
        )


# ─── Facade: primary + fallback + handshake ───────────────────────────────


async def test_evict_suppresses_rtc_rebuild_after_first_failure():
    """REGRESSION: after a peer's PC enters FAILED and _evict_peer
    fires, subsequent ``send()`` calls must NOT re-build the
    handshake — that would hammer the failing path on every send
    (the outbox polls every 5 s; one stuck backlog once produced
    ~4,400 STUN bindings). The 60 s base backoff is the floor that
    keeps the cost bounded from the very FIRST failure; flattening it
    reintroduces the hammering.
    """
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    inst = _fake_instance("peer-1")

    # Stage 1: simulate a PC FAILED event by calling _evict_peer
    # directly (the inbound state-change handler does this in
    # production). The peer must be in _peers first.
    from socialhome.federation.transport import _RtcPeer

    t._peers[inst.id] = _RtcPeer(
        instance_id=inst.id,
        signaling=t._signaling_factory(inst.id),
        inbound=t._inbound_factory(inst.id),
    )
    await t._evict_peer(inst.id)
    # The peer is gone, but suppression is recorded.
    assert inst.id not in t._peers
    assert t._rtc_suppressed(inst.id) is True
    # ...and the window is the 60 s base (± jitter), not zero.
    remaining = t._rtc_suppressed_until[inst.id] - time.monotonic()
    assert remaining >= RTC_RETRY_BACKOFF_BASE_S * (1 - RTC_RETRY_JITTER) - 1
    assert remaining <= RTC_RETRY_BACKOFF_BASE_S * (1 + RTC_RETRY_JITTER)

    # Stage 2: a follow-up send() should go straight to HTTPS and
    # NOT rebuild the handshake. We assert by counting outbound
    # SDP-offer envelopes — _ensure_handshake calls
    # signaling_send(SDP) when it builds a new peer; with
    # suppression the signal must not fire.
    signal.events.clear()
    result = await t.send(instance=inst, envelope_dict={"msg_id": "1"})
    assert result.via == "https"
    # No SDP offer was emitted — _ensure_handshake was skipped.
    offer_calls = [ev for ev in signal.events if ev[1].value == "federation_rtc_offer"]
    assert offer_calls == []


async def test_set_ice_servers_clears_suppression():
    """REGRESSION: an operator who pushes a fresh ICE-server list
    (typically because they just deployed TURN to rescue stuck
    peers) is signalling "I changed something, try again". The
    24 h suppression must clear so the next ``send()`` rebuilds
    the handshake immediately instead of waiting out the timer.
    """
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    # Seed suppression for two peers.
    import time as _time

    expiry = _time.monotonic() + 9999
    t._rtc_suppressed_until = {"peer-a": expiry, "peer-b": expiry}

    t.set_ice_servers(
        [
            {"urls": ["turn:turn.example:3478"], "username": "u", "credential": "c"},
        ]
    )

    assert t._rtc_suppressed_until == {}
    assert t._rtc_suppressed("peer-a") is False
    assert t._rtc_suppressed("peer-b") is False


async def test_set_ice_servers_clears_failure_count():
    """Clearing the suppression window without clearing the failure
    count would only half-honour the operator's "try again" signal:
    the retry fires immediately, but if it fails it lands straight
    back on a 6 h backoff instead of the 60 s base.
    """
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=_RecordingHttpsInbox(),
        signaling_send=_FakeSignaler(),
        rng=lambda: 0.5,
    )
    t._rtc_failure_count = {"peer-a": 7, "peer-b": 3}
    t._rtc_suppressed_until = {"peer-a": time.monotonic() + 9999}

    t.set_ice_servers([dict(_TURN)])

    assert t._rtc_failure_count == {}
    # A failure after the push starts over at the base delay.
    inst = _fake_instance("peer-a")
    t._peers[inst.id] = _RtcPeer(
        instance_id=inst.id,
        signaling=t._signaling_factory(inst.id),
        inbound=t._inbound_factory(inst.id),
    )
    await t._evict_peer(inst.id)
    delay = t._rtc_suppressed_until[inst.id] - time.monotonic()
    assert abs(delay - RTC_RETRY_BACKOFF_BASE_S) < 1.0


# ─── Graduated retry backoff ──────────────────────────────────────────────


async def _evict_once(t: FederationTransport, iid: str) -> float:
    """Drive one FAILED-PC eviction and return the suppression delay."""
    t._peers[iid] = _RtcPeer(
        instance_id=iid,
        signaling=t._signaling_factory(iid),
        inbound=t._inbound_factory(iid),
    )
    await t._evict_peer(iid)
    return t._rtc_suppressed_until[iid] - time.monotonic()


async def test_backoff_grows_geometrically_across_failures():
    """Consecutive FAILED PeerConnections back off 60 s → 4 m → 16 m.

    A transient failure recovers in a minute; a peer that keeps
    failing costs exponentially fewer attempts.
    """
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=_RecordingHttpsInbox(),
        signaling_send=_FakeSignaler(),
        rng=lambda: 0.5,  # jitter factor exactly 1.0
    )
    delays = [await _evict_once(t, "peer-slow") for _ in range(3)]

    assert t._rtc_failure_count["peer-slow"] == 3
    for actual, expected in zip(delays, (60.0, 240.0, 960.0)):
        assert abs(actual - expected) < 1.0, delays


async def test_backoff_is_capped_at_max():
    """The geometric growth stops at the 6 h ceiling — an unreachable
    peer still gets a few attempts a day, and the exponent can't run
    away into weeks."""
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=_RecordingHttpsInbox(),
        signaling_send=_FakeSignaler(),
        rng=lambda: 0.5,
    )
    delays = [await _evict_once(t, "peer-dead") for _ in range(12)]

    assert max(delays) <= RTC_RETRY_BACKOFF_MAX_S + 1.0
    assert abs(delays[-1] - RTC_RETRY_BACKOFF_MAX_S) < 1.0
    # The uncapped value for the 12th failure would be astronomically
    # larger — prove the cap is what bounded it.
    uncapped = RTC_RETRY_BACKOFF_BASE_S * (RTC_RETRY_BACKOFF_FACTOR**11)
    assert uncapped > RTC_RETRY_BACKOFF_MAX_S


async def test_jitter_is_applied_and_bounded():
    """Jitter de-synchronises a household whose peers all failed at
    once, but never escapes ±RTC_RETRY_JITTER of the nominal delay."""
    low = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=_RecordingHttpsInbox(),
        signaling_send=_FakeSignaler(),
        rng=lambda: 0.0,  # jitter factor 1 - RTC_RETRY_JITTER
    )
    high = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=_RecordingHttpsInbox(),
        signaling_send=_FakeSignaler(),
        rng=lambda: 1.0,  # jitter factor 1 + RTC_RETRY_JITTER
    )
    low_delay = await _evict_once(low, "peer-j")
    high_delay = await _evict_once(high, "peer-j")

    assert abs(low_delay - RTC_RETRY_BACKOFF_BASE_S * (1 - RTC_RETRY_JITTER)) < 1.0
    assert abs(high_delay - RTC_RETRY_BACKOFF_BASE_S * (1 + RTC_RETRY_JITTER)) < 1.0
    assert low_delay < high_delay
    # Bounds hold deeper into the schedule too.
    await _evict_once(low, "peer-j")
    low_second = low._rtc_suppressed_until["peer-j"] - time.monotonic()
    nominal = RTC_RETRY_BACKOFF_BASE_S * RTC_RETRY_BACKOFF_FACTOR
    assert nominal * (1 - RTC_RETRY_JITTER) - 1 <= low_second
    assert low_second <= nominal * (1 + RTC_RETRY_JITTER) + 1


async def test_channel_open_clears_failure_count_and_suppression():
    """The channel-open edge is the proof the peer is reachable —
    both the accumulated failure count and any leftover suppression
    window must go."""
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=_RecordingHttpsInbox(),
        signaling_send=_FakeSignaler(),
        rng=lambda: 0.5,
    )
    await _evict_once(t, "peer-ok")
    await _evict_once(t, "peer-ok")
    assert t._rtc_failure_count["peer-ok"] == 2
    assert t._rtc_suppressed("peer-ok") is True

    peer = _RtcPeer(
        instance_id="peer-ok",
        signaling=t._signaling_factory("peer-ok"),
        inbound=t._inbound_factory("peer-ok"),
        on_open=t._on_peer_open,
    )
    # Drive the real open edge rather than calling _on_peer_open
    # directly, so the wiring inside _drain_channel is under test.
    await peer._drain_channel(_OpenThenEndChannel())

    assert "peer-ok" not in t._rtc_failure_count
    assert "peer-ok" not in t._rtc_suppressed_until
    assert t._rtc_suppressed("peer-ok") is False


async def test_next_failure_after_success_restarts_at_base_backoff():
    """A peer that eventually connects must not resume from the
    ceiling it had climbed to — one bad afternoon should not pin a
    now-healthy peer at a 6 h backoff forever."""
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=_RecordingHttpsInbox(),
        signaling_send=_FakeSignaler(),
        rng=lambda: 0.5,
    )
    for _ in range(5):
        await _evict_once(t, "peer-flaky")
    assert (t._rtc_suppressed_until["peer-flaky"] - time.monotonic()) > 600

    peer = _RtcPeer(
        instance_id="peer-flaky",
        signaling=t._signaling_factory("peer-flaky"),
        inbound=t._inbound_factory("peer-flaky"),
        on_open=t._on_peer_open,
    )
    await peer._drain_channel(_OpenThenEndChannel())

    delay = await _evict_once(t, "peer-flaky")
    assert abs(delay - RTC_RETRY_BACKOFF_BASE_S) < 1.0
    assert t._rtc_failure_count["peer-flaky"] == 1


class _OpenThenEndChannel:
    """A DataChannel that opens and then immediately ends its stream."""

    async def wait_open(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


async def test_suppression_expires_after_backoff_window():
    """The suppression is time-bounded — once the backoff window
    has elapsed, the next ``send()`` is allowed to rebuild the
    handshake again. Verified by injecting a tiny backoff window
    so the test doesn't have to wait 24 h."""
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
        rtc_retry_backoff_base_s=0.0,  # immediate retry window
    )
    inst = _fake_instance("peer-x")
    from socialhome.federation.transport import _RtcPeer

    t._peers[inst.id] = _RtcPeer(
        instance_id=inst.id,
        signaling=t._signaling_factory(inst.id),
        inbound=t._inbound_factory(inst.id),
    )
    await t._evict_peer(inst.id)

    # With backoff=0 the suppression window is already past — the
    # check method should report not-suppressed AND prune the
    # entry.
    assert t._rtc_suppressed(inst.id) is False
    assert inst.id not in t._rtc_suppressed_until


async def test_send_uses_rtc_when_peer_is_ready():
    """A peer whose DataChannel is already open takes the RTC path."""
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    inst = _fake_instance("peer-1")

    # Synthesise a ready peer (stub mode never opens the channel
    # on its own).
    peer = _RtcPeer(
        instance_id=inst.id,
        signaling=t._signaling_factory(inst.id),
        inbound=t._inbound_factory(inst.id),
    )

    # Mark the peer ready + attach a fake channel that records sends.
    class _FakeChannel:
        def __init__(self):
            self.sent = []
            self.buffered_amount = 0

        async def send(self, data):
            self.sent.append(data)

    fake_ch = _FakeChannel()
    peer._channel = fake_ch  # type: ignore[attr-defined]
    peer._open.set()  # type: ignore[attr-defined]
    t._peers[inst.id] = peer  # type: ignore[attr-defined]

    result = await t.send(instance=inst, envelope_dict={"msg_id": "x"})

    assert result.ok is True
    assert result.via == "rtc"
    assert fake_ch.sent  # DataChannel received the frame
    assert https_inbox.calls == []  # no HTTPS https_inbox fallback


async def test_send_falls_back_to_inbox_when_peer_not_ready():
    """No RTC channel yet → facade starts a handshake AND uses HTTPS https_inbox."""
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    # Not an ICE-gate test: prime it the way a platform push would.
    t.mark_ice_primed()
    inst = _fake_instance("peer-2")

    result = await t.send(instance=inst, envelope_dict={"msg_id": "x"})

    assert result.ok is True
    assert result.via == "https"
    assert len(https_inbox.calls) == 1
    # Handshake was kicked — one OFFER was sent through the signaler.
    assert (
        signal.events
        and signal.events[0][1] is FederationEventType.FEDERATION_RTC_OFFER
    )


async def test_send_falls_back_when_inbox_fails():
    """HTTPS https_inbox returning non-2xx bubbles up as ``ok=False``."""
    https_inbox = _RecordingHttpsInbox(ok=False, status=502)
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    # Not an ICE-gate test: prime it the way a platform push would.
    t.mark_ice_primed()
    result = await t.send(
        instance=_fake_instance("peer-3"),
        envelope_dict={"msg_id": "x"},
    )
    assert result.ok is False
    assert result.via == "https"
    assert result.status_code == 502


async def test_send_falls_back_to_inbox_when_rtc_send_raises():
    """An RTC send that errors is swallowed; https_inbox delivers instead."""
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    inst = _fake_instance("peer-4")

    class _RaisingPeer(_RtcPeer):
        """Subclass because ``_RtcPeer`` uses ``__slots__`` — we can't
        patch ``.send`` on an instance, so override at the class level.
        """

        @property
        def is_ready(self) -> bool:
            return True

        async def send(self, envelope_dict):
            raise RuntimeError("boom")

    peer = _RaisingPeer(
        instance_id=inst.id,
        signaling=t._signaling_factory(inst.id),
        inbound=t._inbound_factory(inst.id),
    )
    t._peers[inst.id] = peer

    result = await t.send(instance=inst, envelope_dict={"msg_id": "x"})
    assert result.ok is True
    assert result.via == "https"
    assert https_inbox.calls


# ─── Inbound signalling ────────────────────────────────────────────────────


async def test_on_rtc_offer_creates_peer_and_sends_answer():
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    await t.on_rtc_offer(
        from_instance="peer-5",
        payload={"sdp": "v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\n", "sdp_type": "offer"},
    )
    assert "peer-5" in t._peers
    # Answerer posted a FEDERATION_RTC_ANSWER back through the signaler.
    assert any(
        ev[1] is FederationEventType.FEDERATION_RTC_ANSWER for ev in signal.events
    )


async def test_on_rtc_offer_ignores_empty_sdp():
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    await t.on_rtc_offer(from_instance="peer-6", payload={"sdp": ""})
    # Peer was still registered (we hold the slot) but no ANSWER sent.
    assert not any(
        ev[1] is FederationEventType.FEDERATION_RTC_ANSWER for ev in signal.events
    )


async def test_on_rtc_answer_with_matching_from_applies():
    """S-14: the answer origin must match the pending-offer target."""
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    # Not an ICE-gate test: prime it the way a platform push would.
    t.mark_ice_primed()
    inst = _fake_instance("peer-7")
    # Prime the peer with a pending offer.
    await t._ensure_handshake(inst)

    await t.on_rtc_answer(
        from_instance="peer-7",
        payload={"sdp": "answer-sdp", "sdp_type": "answer"},
    )
    peer = t._peers["peer-7"]
    assert peer._expected_answer_from is None  # type: ignore[attr-defined]


async def test_on_rtc_answer_with_mismatched_from_is_rejected():
    """S-14: an answer from the wrong peer must NOT be applied."""
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    # Not an ICE-gate test: prime it the way a platform push would.
    t.mark_ice_primed()
    inst = _fake_instance("peer-8")
    await t._ensure_handshake(inst)

    await t.on_rtc_answer(
        from_instance="attacker",
        payload={"sdp": "evil", "sdp_type": "answer"},
    )
    peer = t._peers["peer-8"]
    # Still expecting the real peer's answer.
    assert peer._expected_answer_from == "peer-8"  # type: ignore[attr-defined]


async def test_on_rtc_answer_unknown_peer_is_noop():
    """Answer for a peer we never offered to is dropped silently."""
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    await t.on_rtc_answer(
        from_instance="ghost",
        payload={"sdp": "x"},
    )
    assert t.peer_count() == 0


async def test_on_rtc_ice_unknown_peer_creates_buffering_stub(monkeypatch):
    """ICE arriving before the matching OFFER is buffered in a stub
    :class:`_RtcPeer` so a later OFFER can flush it — previously the
    candidate was silently dropped, stranding ICE with no remote
    candidates and failing the WebRTC connectivity timer."""
    from socialhome.federation import transport as transport_mod

    # Short timeout so the buffered candidate doesn't park 10s and
    # leave a lingering task at teardown.
    monkeypatch.setattr(transport_mod, "ICE_BUFFER_TIMEOUT_S", 0.05)

    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    # Should not raise — stub peer is created and buffers the candidate.
    await t.on_rtc_ice(
        from_instance="ghost",
        payload={"candidate": "c", "sdp_mid": "0"},
    )
    # The stub peer is registered so a follow-up OFFER will reuse it.
    assert "ghost" in t._peers


async def test_on_rtc_ice_accepts_trickled_candidate():
    """A candidate arriving after the remote description has been applied
    flushes straight through to the PC. Uses a stub peer to avoid
    spinning up a real aiolibdatachannel PeerConnection (whose
    iterator tasks the pytest_homeassistant_custom_component plugin
    would flag at teardown)."""
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )

    applied: list[tuple[str, str]] = []

    class _StubPc:
        async def add_remote_candidate(self, candidate, sdp_mid):
            applied.append((candidate, sdp_mid))

    # Pre-seed a stub peer that pretends its remote description is
    # already in. ``on_rtc_ice`` will find it in ``_peers`` and forward
    # the candidate directly.
    peer = _RtcPeer(
        instance_id="peer-9",
        signaling=t._signaling_factory("peer-9"),
        inbound=t._inbound_factory("peer-9"),
    )
    peer._pc = _StubPc()
    peer._remote_description_applied.set()
    t._peers["peer-9"] = peer

    await t.on_rtc_ice(
        from_instance="peer-9",
        payload={
            "candidate": "candidate:1 udp 1 1.1.1.1 5000 typ host",
            "sdp_mid": "0",
        },
    )
    assert applied == [("candidate:1 udp 1 1.1.1.1 5000 typ host", "0")]


# ─── Wire-ordering invariant: SDP before any trickled ICE ─────────────────
#
# Real-world regression: ``_drain_ice`` was spawned *before*
# :meth:`_RtcPeer.accept_offer` (and ``start_offer``) awaited the SDP
# signaling. The drain task and the SDP signaling were independent HTTPS
# posts to the peer's inbox, so a candidate POST could outrun the
# OFFER/ANSWER POST on the wire. The offerer (us) then dropped the
# candidate at :data:`ICE_BUFFER_TIMEOUT_S` because its remote description
# hadn't been applied yet, and ICE connectivity check failed with zero
# remote candidates. federation-demo missed it because loopback HTTPS
# latency is microseconds and the ANSWER always won the race.


class _RacingPeerConnection(rtc.PeerConnection):  # type: ignore[misc]
    """Fake PC whose ICE gathering emits a candidate the moment
    ``set_local_description`` starts — models libdatachannel surfacing
    host candidates immediately on the gathering pass, which is exactly
    when the race window opens.
    """

    def __init__(self, config=None) -> None:
        super().__init__(config)
        self._ice_queue: asyncio.Queue = asyncio.Queue()

    async def set_local_description(self, type_: str = "offer"):
        self._ice_queue.put_nowait(
            rtc.IceCandidate(
                "candidate:host 1 udp 1 1.1.1.1 5000 typ host",
                "0",
            ),
        )
        return await super().set_local_description(type_)

    async def ice_candidates(self):
        while not self._closed:
            cand = await self._ice_queue.get()
            if cand is None:
                return
            yield cand

    def close(self) -> None:
        try:
            self._ice_queue.put_nowait(None)
        except Exception:  # noqa: BLE001
            pass
        super().close()


async def _settle_drain(signal: "_FakeSignaler", *, attempts: int = 20) -> None:
    """Yield until the drain task has emitted at least one ICE event
    (or we run out of patience). Deterministic on the fake event loop —
    a handful of ``sleep(0)`` cycles is enough because the fake signaler
    completes synchronously."""
    for _ in range(attempts):
        if any(ev[1] is FederationEventType.FEDERATION_RTC_ICE for ev in signal.events):
            return
        await asyncio.sleep(0)


async def test_accept_offer_signals_answer_before_ice_candidates(monkeypatch):
    """Answerer side: ANSWER must hit the signaler before any ICE.

    If the drain task starts before ``_signaling(ANSWER, ...)`` is
    awaited, candidate POSTs can win the race to the offerer's inbox,
    and the offerer drops them at :data:`ICE_BUFFER_TIMEOUT_S` before
    its remote description has been applied.
    """
    monkeypatch.setattr(rtc, "PeerConnection", _RacingPeerConnection)

    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )

    await t.on_rtc_offer(
        from_instance="peer-race-a",
        payload={
            "sdp": "v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\n",
            "sdp_type": "offer",
        },
    )
    await _settle_drain(signal)

    event_types = [ev[1] for ev in signal.events]
    answer_idx = event_types.index(FederationEventType.FEDERATION_RTC_ANSWER)
    ice_indices = [
        i
        for i, et in enumerate(event_types)
        if et is FederationEventType.FEDERATION_RTC_ICE
    ]
    assert ice_indices, "drain task should have flushed the host candidate"
    assert all(answer_idx < i for i in ice_indices), (
        f"wire order broken: ANSWER at {answer_idx}, "
        f"ICE candidates at {ice_indices} — the answerer raced ICE "
        "ahead of ANSWER (signaling race in _RtcPeer.accept_offer)"
    )

    await t.close_all()


async def test_start_offer_signals_offer_before_ice_candidates(monkeypatch):
    """Offerer side: OFFER must hit the signaler before any ICE.

    Symmetric to the answerer invariant — the receiver buffers ICE that
    overtakes an OFFER, but only inside :data:`ICE_BUFFER_TIMEOUT_S`,
    so out-of-order trickle on a slow link still fails.
    """
    monkeypatch.setattr(rtc, "PeerConnection", _RacingPeerConnection)

    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    # Not an ICE-gate test: prime it the way a platform push would.
    t.mark_ice_primed()

    await t._ensure_handshake(_fake_instance("peer-race-o"))
    await _settle_drain(signal)

    event_types = [ev[1] for ev in signal.events]
    offer_idx = event_types.index(FederationEventType.FEDERATION_RTC_OFFER)
    ice_indices = [
        i
        for i, et in enumerate(event_types)
        if et is FederationEventType.FEDERATION_RTC_ICE
    ]
    assert ice_indices, "drain task should have flushed the host candidate"
    assert all(offer_idx < i for i in ice_indices), (
        f"wire order broken: OFFER at {offer_idx}, "
        f"ICE candidates at {ice_indices} — the offerer raced ICE "
        "ahead of OFFER (signaling race in _RtcPeer.start_offer)"
    )

    await t.close_all()


# ─── Facade lifecycle ──────────────────────────────────────────────────────


async def test_close_peer_removes_entry():
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    # Not an ICE-gate test: prime it the way a platform push would.
    t.mark_ice_primed()
    await t._ensure_handshake(_fake_instance("peer-10"))
    assert t.peer_count() == 1
    await t.close_peer("peer-10")
    assert t.peer_count() == 0


async def test_close_all_drops_every_peer():
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    # Not an ICE-gate test: prime it the way a platform push would.
    t.mark_ice_primed()
    await t._ensure_handshake(_fake_instance("a"))
    await t._ensure_handshake(_fake_instance("b"))
    await t.close_all()
    assert t.peer_count() == 0


async def test_is_ready_reports_false_for_unknown_peer():
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    assert t.is_ready("never-seen") is False


# ─── HttpsInboxTransport ──────────────────────────────────────────────────────


async def test_https_inbox_transport_2xx_is_ok():
    """HttpsInboxTransport.send returns (True, status) for 2xx."""

    class _FakeResp:
        def __init__(self, status):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeClient:
        def post(self, url, json, timeout):
            return _FakeResp(204)

    async def _factory():
        return _FakeClient()

    wt = HttpsInboxTransport(client_factory=_factory)
    ok, status = await wt.send(
        instance=_fake_instance("peer"),
        envelope_dict={"msg_id": "x"},
    )
    assert ok is True and status == 204


async def test_https_inbox_transport_non_2xx_is_failure():
    class _FakeResp:
        def __init__(self, status):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeClient:
        def post(self, url, json, timeout):
            return _FakeResp(503)

    async def _factory():
        return _FakeClient()

    wt = HttpsInboxTransport(client_factory=_factory)
    ok, status = await wt.send(
        instance=_fake_instance("peer"),
        envelope_dict={"x": 1},
    )
    assert ok is False and status == 503


async def test_https_inbox_transport_network_error_is_failure():
    class _RaisingClient:
        def post(self, *a, **kw):
            raise RuntimeError("boom")

    async def _factory():
        return _RaisingClient()

    wt = HttpsInboxTransport(client_factory=_factory)
    ok, status = await wt.send(
        instance=_fake_instance("peer"),
        envelope_dict={"x": 1},
    )
    assert ok is False and status is None


# ─── PeerTransportChanged publication ─────────────────────────────────────


async def test_rtc_peer_publishes_transport_changed_on_open():
    """Opening the DataChannel publishes PeerTransportChanged(transport='rtc')."""
    bus = EventBus()
    received: list[PeerTransportChanged] = []

    async def _record(e: PeerTransportChanged) -> None:
        received.append(e)

    bus.subscribe(PeerTransportChanged, _record)

    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
        bus=bus,
    )

    peer = _RtcPeer(
        instance_id="peer-tx",
        signaling=t._signaling_factory("peer-tx"),
        inbound=t._inbound_factory("peer-tx"),
        bus=bus,
    )
    # Simulate the open edge — set the asyncio.Event the way
    # _drain_channel does once the channel reports OPEN.
    peer._open.set()
    await peer._publish_open_if_needed()

    assert len(received) == 1
    assert received[0].instance_id == "peer-tx"
    assert received[0].transport == "rtc"


async def test_rtc_peer_publishes_transport_changed_on_close():
    """Closing the peer (after a successful open) publishes
    PeerTransportChanged(transport='https')."""
    bus = EventBus()
    received: list[PeerTransportChanged] = []

    async def _record(e: PeerTransportChanged) -> None:
        received.append(e)

    bus.subscribe(PeerTransportChanged, _record)

    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
        bus=bus,
    )
    peer = _RtcPeer(
        instance_id="peer-cx",
        signaling=t._signaling_factory("peer-cx"),
        inbound=t._inbound_factory("peer-cx"),
        bus=bus,
    )
    peer._open.set()
    peer._loop = asyncio.get_running_loop()
    await peer._publish_open_if_needed()
    received.clear()

    peer.close()
    # close() schedules a task — yield to let it run.
    await asyncio.sleep(0)

    assert len(received) == 1
    assert received[0].instance_id == "peer-cx"
    assert received[0].transport == "https"


async def test_rtc_peer_does_not_publish_close_without_prior_open():
    """A peer that never opened doesn't publish a spurious 'https' on close.
    The transport never *flipped* — it was always HTTPS."""
    bus = EventBus()
    received: list[PeerTransportChanged] = []

    async def _record(e: PeerTransportChanged) -> None:
        received.append(e)

    bus.subscribe(PeerTransportChanged, _record)

    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
        bus=bus,
    )
    peer = _RtcPeer(
        instance_id="peer-stub",
        signaling=t._signaling_factory("peer-stub"),
        inbound=t._inbound_factory("peer-stub"),
        bus=bus,
    )
    peer.close()
    await asyncio.sleep(0)
    assert received == []


async def test_rtc_peer_close_is_idempotent():
    """A double close() after a prior open publishes 'https' exactly once,
    not twice — the second close() is a no-op for the bus."""
    bus = EventBus()
    received: list[PeerTransportChanged] = []

    async def _record(e: PeerTransportChanged) -> None:
        received.append(e)

    bus.subscribe(PeerTransportChanged, _record)

    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
        bus=bus,
    )
    peer = _RtcPeer(
        instance_id="peer-double-close",
        signaling=t._signaling_factory("peer-double-close"),
        inbound=t._inbound_factory("peer-double-close"),
        bus=bus,
    )
    peer._loop = asyncio.get_running_loop()
    peer._open.set()
    await peer._publish_open_if_needed()
    received.clear()

    peer.close()
    peer.close()  # second call — must not publish again

    # Let the create_task'd publishes settle.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(received) == 1
    assert received[0].instance_id == "peer-double-close"
    assert received[0].transport == "https"


# ─── Perfect negotiation (glare resolution) ───────────────────────────────
#
# When two paired peers both fire ``start_offer`` simultaneously, both
# sides POST an OFFER via HTTPS inbox. Without perfect negotiation both
# sides' ``accept_offer`` would clobber their own pending PeerConnection
# and ICE would never converge. The tests below pin the correct behaviour:
#
#  - impolite side (lex-smaller own_id > peer_id = False) ignores
#    the incoming OFFER and keeps its own pending offer alive.
#  - polite side (lex-smaller own_id > peer_id = True) rolls back its
#    pending offer and accepts the impolite peer's OFFER instead.
#  - After rollback a stale ANSWER (for the polite side's now-cancelled
#    offer) is silently dropped.
#  - The politeness role is symmetric and deterministic for any pair
#    of instance-ids.

_STUB_SDP = "v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\na=mock\r\n"


async def _make_peer(
    own_id: str,
    peer_id: str,
    *,
    signal: _FakeSignaler | None = None,
) -> tuple[_RtcPeer, _FakeSignaler]:
    """Helper: create a ``_RtcPeer`` for *peer_id* as seen from *own_id*."""
    if signal is None:
        signal = _FakeSignaler()

    async def _signaling(et, payload):
        signal.events.append((peer_id, et, payload))
        return DeliveryResult(instance_id=peer_id, ok=True, status_code=200)

    async def _inbound(_envelope):
        return None

    peer = _RtcPeer(
        instance_id=peer_id,
        signaling=_signaling,
        inbound=_inbound,
        polite=own_id > peer_id,
    )
    return peer, signal


async def test_politeness_role_assignment():
    """Lex comparison is deterministic regardless of call-site perspective.

    If own_id="aaaa" and peer_id="bbbb":
      - A's perspective: own "aaaa" > peer "bbbb" = False → A is impolite
      - B's perspective: own "bbbb" > peer "aaaa" = True  → B is polite
    """
    peer_a_for_b, _ = await _make_peer("aaaa", "bbbb")  # A's _RtcPeer for B
    peer_b_for_a, _ = await _make_peer("bbbb", "aaaa")  # B's _RtcPeer for A

    assert peer_a_for_b._polite is False, "A (smaller id) should be impolite"
    assert peer_b_for_a._polite is True, "B (larger id) should be polite"


async def test_no_glare_baseline():
    """Only A initiates; B receives the OFFER and replies normally.

    Assert: A ends in have-local-offer (after start_offer), B ends in
    have-local-answer (after accept_offer), no rollback logged.
    """
    signal_a = _FakeSignaler()
    signal_b = _FakeSignaler()
    peer_a, _ = await _make_peer("aaaa", "bbbb", signal=signal_a)
    peer_b, _ = await _make_peer("bbbb", "aaaa", signal=signal_b)

    # A starts an offer.
    await peer_a.start_offer()
    assert peer_a._pc.signaling_state == rtc.SignalingState.HAVE_LOCAL_OFFER  # type: ignore[union-attr]
    assert peer_a._making_offer is True

    # B receives A's offer and replies.
    offer_sdp = _STUB_SDP
    await peer_b.accept_offer(sdp=offer_sdp, from_instance="aaaa")
    assert peer_b._pc is not None
    # The lib (and the stub) collapses "have-local-answer" straight
    # into STABLE the moment the answer is set locally.
    assert peer_b._pc.signaling_state == rtc.SignalingState.STABLE

    # Verify no rollback log: peer_b sent an ANSWER, not just silence.
    answer_events = [
        e for e in signal_b.events if e[1] is FederationEventType.FEDERATION_RTC_ANSWER
    ]
    assert answer_events, "B should have sent an ANSWER"

    # A applies B's answer.
    ok = await peer_a.apply_answer(sdp=_STUB_SDP, from_instance="bbbb")
    assert ok is True
    assert peer_a._making_offer is False

    peer_a.close()
    peer_b.close()


async def test_glare_impolite_ignores_incoming_offer():
    """Impolite side (own_id < peer_id) ignores an incoming OFFER while
    making its own offer.  The impolite PC stays in have-local-offer and
    no ANSWER is posted.
    """
    # A ("aaaa") is impolite: "aaaa" > "bbbb" is False.
    signal_a = _FakeSignaler()
    peer_a, _ = await _make_peer("aaaa", "bbbb", signal=signal_a)

    # A starts its own offer.
    await peer_a.start_offer()
    assert peer_a._making_offer is True
    assert peer_a._pc.signaling_state == rtc.SignalingState.HAVE_LOCAL_OFFER  # type: ignore[union-attr]

    # B's OFFER arrives — impolite A should ignore it.
    await peer_a.accept_offer(sdp=_STUB_SDP, from_instance="bbbb")

    # A's PC must still be the original one (in have-local-offer state).
    assert peer_a._pc is not None
    assert peer_a._pc.signaling_state == rtc.SignalingState.HAVE_LOCAL_OFFER  # type: ignore[union-attr]
    assert peer_a._making_offer is True  # still in "making offer" window

    # A must NOT have sent an ANSWER.
    answer_events = [
        e for e in signal_a.events if e[1] is FederationEventType.FEDERATION_RTC_ANSWER
    ]
    assert not answer_events, "Impolite side must not send an ANSWER on glare"

    peer_a.close()


async def test_glare_polite_side_rolls_back_and_accepts():
    """Polite side (own_id > peer_id) rolls back its pending offer and
    accepts the impolite peer's incoming OFFER.

    Sequence:
      1. B starts an offer (B is polite: "bbbb" > "aaaa").
      2. B receives A's OFFER while its own is pending → rollback.
      3. B builds a fresh answerer PC and sends an ANSWER.
    """
    signal_b = _FakeSignaler()
    peer_b, _ = await _make_peer("bbbb", "aaaa", signal=signal_b)

    # B starts its own offer.
    await peer_b.start_offer()
    original_pc = peer_b._pc
    assert peer_b._making_offer is True
    assert original_pc.signaling_state == rtc.SignalingState.HAVE_LOCAL_OFFER  # type: ignore[union-attr]

    # A's OFFER arrives while B is making an offer → polite rollback.
    await peer_b.accept_offer(sdp=_STUB_SDP, from_instance="aaaa")

    # B must have built a NEW PC (the original was closed on rollback).
    assert peer_b._pc is not original_pc, "Polite side must create a fresh PC"
    assert peer_b._making_offer is False, "making_offer must be cleared after rollback"

    # B must have sent an ANSWER.
    answer_events = [
        e for e in signal_b.events if e[1] is FederationEventType.FEDERATION_RTC_ANSWER
    ]
    assert answer_events, "Polite side must send an ANSWER after rollback"

    peer_b.close()


async def test_late_answer_after_rollback_is_ignored():
    """After polite rollback, a stale ANSWER arriving for the cancelled
    offer is silently dropped — no exception, no state corruption.
    """
    signal_b = _FakeSignaler()
    peer_b, _ = await _make_peer("bbbb", "aaaa", signal=signal_b)

    # B starts offer, then rolls back by accepting A's offer.
    await peer_b.start_offer()
    await peer_b.accept_offer(sdp=_STUB_SDP, from_instance="aaaa")

    # Now B is in answerer mode; a late ANSWER for B's cancelled offer
    # arrives.  The PC is no longer in have-local-offer → ignored.
    ok = await peer_b.apply_answer(sdp=_STUB_SDP, from_instance="aaaa")
    assert ok is False, "Stale ANSWER after rollback must be ignored"

    peer_b.close()


async def test_full_glare_resolution_end_to_end():
    """Full glare scenario: A and B both call start_offer simultaneously.

    A ("aaaa") is impolite; B ("bbbb") is polite.

    Expected outcome:
      - A ignores B's OFFER (impolite path).
      - B rolls back and sends ANSWER for A's OFFER (polite path).
      - A applies B's ANSWER; ICE converges on one OFFER→ANSWER cycle.
      - Only one ANSWER is delivered end-to-end.
    """
    signal_a = _FakeSignaler()
    signal_b = _FakeSignaler()
    peer_a, _ = await _make_peer("aaaa", "bbbb", signal=signal_a)
    peer_b, _ = await _make_peer("bbbb", "aaaa", signal=signal_b)

    # Both sides fire start_offer "simultaneously".
    await peer_a.start_offer()
    await peer_b.start_offer()

    # Cross-deliver the offers.
    await peer_a.accept_offer(sdp=_STUB_SDP, from_instance="bbbb")  # A ignores
    await peer_b.accept_offer(sdp=_STUB_SDP, from_instance="aaaa")  # B rolls back

    # A's PC must still be the original offerer (have-local-offer).
    assert peer_a._pc is not None
    assert peer_a._pc.signaling_state == rtc.SignalingState.HAVE_LOCAL_OFFER  # type: ignore[union-attr]
    assert peer_a._making_offer is True

    # B must have sent exactly one ANSWER.
    b_answers = [
        e for e in signal_b.events if e[1] is FederationEventType.FEDERATION_RTC_ANSWER
    ]
    assert len(b_answers) == 1, (
        f"Expected exactly one ANSWER from B, got {len(b_answers)}"
    )

    # A applies B's ANSWER.
    ok = await peer_a.apply_answer(sdp=_STUB_SDP, from_instance="bbbb")
    assert ok is True
    assert peer_a._making_offer is False

    # No ANSWER from A (impolite; kept its own offer).
    a_answers = [
        e for e in signal_a.events if e[1] is FederationEventType.FEDERATION_RTC_ANSWER
    ]
    assert not a_answers, "Impolite side must never send an ANSWER on glare"

    peer_a.close()
    peer_b.close()


async def test_no_glare_on_subsequent_send():
    """After pairing, a second ``transport.send`` does NOT re-trigger
    ``_ensure_handshake`` — the peer is already in ``_peers``.
    """
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    # Not an ICE-gate test: prime it the way a platform push would.
    t.mark_ice_primed()
    inst = _fake_instance("peer-ng")

    # First send: no peer yet → creates peer + starts handshake.
    await t.send(instance=inst, envelope_dict={"msg_id": "1"})
    offer_count_after_first = sum(
        1 for e in signal.events if e[1] is FederationEventType.FEDERATION_RTC_OFFER
    )
    assert offer_count_after_first == 1, "First send must trigger exactly one OFFER"
    assert "peer-ng" in t._peers

    # Second send: peer already registered → no new handshake, no new OFFER.
    await t.send(instance=inst, envelope_dict={"msg_id": "2"})
    offer_count_after_second = sum(
        1 for e in signal.events if e[1] is FederationEventType.FEDERATION_RTC_OFFER
    )
    assert offer_count_after_second == 1, "Second send must NOT trigger another OFFER"


# ─── Lazy ICE-server resolution ───────────────────────────────────────────


_TURN = {"urls": "turn:relay.example:3478", "username": "u", "credential": "c"}


async def _noop_signaling(_evt, _payload) -> None:
    return None


async def _noop_inbound(_data) -> None:
    return None


async def test_peer_builds_pc_with_current_ice_list_not_construction_snapshot():
    """The offerer reads the ICE list when it builds the PeerConnection.

    Under haos the TURN credentials only land once ``HaIceServerSync``
    has run, which can be after a boot-backlog peer was constructed —
    snapshotting at construction time left that peer STUN-only forever.
    """
    servers: list[dict] = []
    peer = _RtcPeer(
        instance_id="peer-lazy-offer",
        ice_provider=lambda: servers,
        signaling=_noop_signaling,
        inbound=_noop_inbound,
    )

    # TURN credentials arrive *after* the peer object exists.
    servers.append(_TURN)

    await peer.start_offer()

    urls = [s.url for s in peer._pc._config.ice_servers]
    assert "turn:relay.example:3478" in urls
    peer.close()


async def test_answerer_builds_pc_with_current_ice_list():
    """Same lazy read on the answerer path (``accept_offer``)."""
    servers: list[dict] = []
    peer = _RtcPeer(
        instance_id="peer-lazy-answer",
        ice_provider=lambda: servers,
        signaling=_noop_signaling,
        inbound=_noop_inbound,
    )

    servers.append(_TURN)

    await peer.accept_offer(sdp=_STUB_SDP, from_instance="peer-lazy-answer")

    urls = [s.url for s in peer._pc._config.ice_servers]
    assert "turn:relay.example:3478" in urls
    peer.close()


# ─── ICE generation: rebuilding peers stuck on a stale ICE list ───────────


_TURN_2 = {
    "urls": "turn:relay2.example:3478",
    "username": "u2",
    "credential": "c2",
}


def _stub_peer(t: FederationTransport, iid: str) -> _RtcPeer:
    """An un-started peer registered in the transport, as the boot-time
    outbox drain leaves it before the TURN credentials land."""
    peer = _RtcPeer(
        instance_id=iid,
        ice_provider=t._current_ice_servers,
        signaling=t._signaling_factory(iid),
        inbound=t._inbound_factory(iid),
        ice_generation=t._ice_generation,
    )
    t._peers[iid] = peer
    return peer


def _offers(signal: _FakeSignaler) -> list:
    return [
        ev for ev in signal.events if ev[1] is FederationEventType.FEDERATION_RTC_OFFER
    ]


async def test_set_ice_servers_rebuilds_unconnected_peers_on_next_send():
    """REGRESSION (haos TURN startup race): a peer built before the
    Cloudflare TURN credentials landed stayed STUN-only for the whole
    process lifetime. The next ``send()`` must retire it and rebuild
    the handshake so the new PeerConnection picks up TURN.
    """
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    inst = _fake_instance("peer-stale")
    old = _stub_peer(t, inst.id)

    t.set_ice_servers([_TURN])
    signal.events.clear()

    await t.send(instance=inst, envelope_dict={"msg_id": "1"})

    assert len(_offers(signal)) == 1
    new = t._peers[inst.id]
    assert new is not old
    assert old._closed is True
    assert new.ice_generation == t._ice_generation
    await t.close_all()


async def test_set_ice_servers_leaves_connected_peers_alone():
    """A peer with an open DataChannel works — tearing it down to pick
    up new TURN credentials it no longer needs would be pure churn.
    """
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    inst = _fake_instance("peer-open")
    peer = _stub_peer(t, inst.id)
    peer._open.set()

    class _Chan:
        buffered_amount = 0

        async def send(self, _data):
            return None

    peer._channel = _Chan()

    t.set_ice_servers([_TURN])
    signal.events.clear()

    result = await t.send(instance=inst, envelope_dict={"msg_id": "1"})

    assert result.via == "rtc"
    assert _offers(signal) == []
    assert t._peers[inst.id] is peer
    assert peer._closed is False


async def test_unchanged_ice_list_does_not_bump_generation():
    """HA re-pushes a byte-identical list on every daily refresh —
    churning every unconnected peer once a day would be a self-inflicted
    outage. Equality is on content, not object identity or ordering.
    """
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
        ice_servers=[dict(_TURN), dict(_TURN_2)],
    )
    inst = _fake_instance("peer-same")
    peer = _stub_peer(t, inst.id)
    gen_before = t._ice_generation

    # Freshly-constructed, equal-content, reordered list.
    t.set_ice_servers([dict(_TURN_2), dict(_TURN)])

    assert t._ice_generation == gen_before
    assert peer.needs_rehandshake(t._ice_generation) is False

    await t.send(instance=inst, envelope_dict={"msg_id": "1"})
    assert t._peers[inst.id] is peer
    assert peer._closed is False


async def test_changed_ice_list_bumps_generation():
    """The positive direction — a real update must not be swallowed by
    the unchanged-list early return.
    """
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
        ice_servers=[dict(_TURN)],
    )
    gen_before = t._ice_generation

    # Same URL, rotated credential — connectivity-relevant.
    t.set_ice_servers([{**_TURN, "credential": "rotated"}])
    assert t._ice_generation == gen_before + 1

    # Added server.
    t.set_ice_servers([{**_TURN, "credential": "rotated"}, dict(_TURN_2)])
    assert t._ice_generation == gen_before + 2

    # Removed server.
    t.set_ice_servers([dict(_TURN_2)])
    assert t._ice_generation == gen_before + 3


async def test_retired_peer_is_not_suppressed():
    """Retirement is our own decision, not a peer failure — it must not
    stamp ``_rtc_suppressed_until`` (which would block the rebuild for
    24 h and re-create the very outage this fixes).
    """
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    # Not an ICE-gate test: prime it the way a platform push would.
    t.mark_ice_primed()
    inst = _fake_instance("peer-retire")
    peer = _stub_peer(t, inst.id)

    await t._retire_peer(inst.id, peer)

    assert inst.id not in t._peers
    assert inst.id not in t._rtc_suppressed_until
    assert t._rtc_suppressed(inst.id) is False

    signal.events.clear()
    await t.send(instance=inst, envelope_dict={"msg_id": "1"})
    assert len(_offers(signal)) == 1
    await t.close_all()


async def test_rebuilt_peer_does_not_retire_again():
    """The rebuilt peer carries the current generation, so the predicate
    is immediately false — no rebuild loop on every send().
    """
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    inst = _fake_instance("peer-once")
    _stub_peer(t, inst.id)

    t.set_ice_servers([_TURN])
    signal.events.clear()

    await t.send(instance=inst, envelope_dict={"msg_id": "1"})
    rebuilt = t._peers[inst.id]
    await t.send(instance=inst, envelope_dict={"msg_id": "2"})

    assert len(_offers(signal)) == 1
    assert t._peers[inst.id] is rebuilt
    assert rebuilt._closed is False
    await t.close_all()


# ─── _retire_peer identity scoping ────────────────────────────────────────


async def test_retire_peer_only_pops_the_object_the_caller_captured():
    """REGRESSION: retirement is identity-scoped, not id-scoped.

    Two concurrent ``send()`` calls to the same instance (a live send
    overlapping an outbox redelivery) both capture peer P1 and both
    decide to retire it. Caller A retires P1, ``_ensure_handshake``
    inserts P2, then releases ``_lock`` to await ``start_offer()``. If
    caller B popped by id it would close P2 mid-handshake — handshake
    churn and, worst case, a FAILED PC that then gets suppressed.

    Driven deterministically: the test holds ``_lock`` (standing in for
    caller A's critical section), parks caller B inside ``_retire_peer``
    on that lock, then performs exactly the mutation ``_ensure_handshake``
    makes under the lock (swap P1 for P2) before releasing.
    """
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    inst = _fake_instance("peer-identity")
    p1 = _stub_peer(t, inst.id)

    await t._lock.acquire()
    b = asyncio.create_task(t._retire_peer(inst.id, p1))
    for _ in range(3):
        await asyncio.sleep(0)
    assert not b.done()  # parked on the lock, P1 not popped yet

    # What caller A's ``_ensure_handshake`` does under the same lock.
    del t._peers[inst.id]
    p1.close()
    p2 = _RtcPeer(
        instance_id=inst.id,
        ice_provider=t._current_ice_servers,
        signaling=t._signaling_factory(inst.id),
        inbound=t._inbound_factory(inst.id),
        ice_generation=t._ice_generation,
    )
    t._peers[inst.id] = p2
    t._lock.release()

    await b

    assert t._peers[inst.id] is p2
    assert p2._closed is False
    await t.close_all()


async def test_retire_peer_ignores_a_stale_peer_object():
    """The straight-line form of the same guard: retiring an object the
    map no longer holds must close nothing and pop nothing.
    """
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    inst = _fake_instance("peer-stale-obj")
    stale = _stub_peer(t, inst.id)
    current = _stub_peer(t, inst.id)  # replaces ``stale`` in the map

    await t._retire_peer(inst.id, stale)

    assert t._peers[inst.id] is current
    assert current._closed is False
    await t.close_all()


async def test_retire_peer_unknown_instance_id_returns_cleanly():
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    orphan = _RtcPeer(
        instance_id="never-registered",
        ice_provider=t._current_ice_servers,
        signaling=t._signaling_factory("never-registered"),
        inbound=t._inbound_factory("never-registered"),
    )

    await t._retire_peer("never-registered", orphan)

    assert t._peers == {}
    assert orphan._closed is False


async def test_overlapping_sends_leave_one_open_peer():
    """Two sends racing on the same stale peer must converge on a single
    live peer and a single handshake — never a map entry holding a peer
    somebody already closed.
    """
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    inst = _fake_instance("peer-race")
    old = _stub_peer(t, inst.id)

    t.set_ice_servers([_TURN])
    signal.events.clear()

    await asyncio.gather(
        t.send(instance=inst, envelope_dict={"msg_id": "1"}),
        t.send(instance=inst, envelope_dict={"msg_id": "2"}),
    )

    assert len(t._peers) == 1
    survivor = t._peers[inst.id]
    assert survivor is not old
    assert survivor._closed is False
    assert len(_offers(signal)) == 1
    await t.close_all()


# ─── Suppression clearing is unconditional ────────────────────────────────


async def test_unchanged_ice_list_still_clears_suppressions():
    """REGRESSION: the suppression clear is the only "operator changed
    something, try again" lever in the codebase, and ``HaIceServerSync``
    re-applies the SAME list on its 24 h poll. Gating the clear on a
    content change left a peer suppressed after a FAILED PC stuck on
    HTTPS whenever HA returned identical credentials.
    """
    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
        ice_servers=[dict(_TURN)],
    )
    inst = _fake_instance("peer-suppressed")
    peer = _stub_peer(t, inst.id)
    gen_before = t._ice_generation
    t._rtc_suppressed_until[inst.id] = 1e18  # effectively forever

    t.set_ice_servers([dict(_TURN)])

    assert t._rtc_suppressed_until == {}
    assert t._rtc_suppressed(inst.id) is False
    # ...but an unchanged list is still not a reason to churn peers.
    assert t._ice_generation == gen_before
    assert t._peers[inst.id] is peer
    assert peer._closed is False

    signal.events.clear()
    await t.send(instance=inst, envelope_dict={"msg_id": "1"})
    assert t._peers[inst.id] is peer


# ─── _ice_fingerprint ─────────────────────────────────────────────────────


def _config_key(servers: list[dict]) -> frozenset[tuple]:
    """Connectivity-relevant content of what ``_build_rtc_config`` emits."""
    cfg = _build_rtc_config(servers)
    return frozenset((s.url, s.username, s.credential) for s in cfg.ice_servers)


def test_ice_fingerprint_string_and_list_urls_are_equal():
    """``urls`` may be a bare string or a list — both reach
    ``_build_rtc_config`` identically, so they must fingerprint equal.
    """
    one = [{"urls": "turn:a:3478", "username": "u", "credential": "c"}]
    other = [{"urls": ["turn:a:3478"], "username": "u", "credential": "c"}]
    assert _ice_fingerprint(one) == _ice_fingerprint(other)
    assert _config_key(one) == _config_key(other)


def test_ice_fingerprint_differs_on_credential():
    """The daily TURN credential rotation is the whole point — swallowing
    it would leave every peer on expired credentials.
    """
    before = [{"urls": "turn:a:3478", "username": "u", "credential": "c1"}]
    after = [{"urls": "turn:a:3478", "username": "u", "credential": "c2"}]
    assert _ice_fingerprint(before) != _ice_fingerprint(after)


def test_ice_fingerprint_differs_on_username():
    before = [{"urls": "turn:a:3478", "username": "u1", "credential": "c"}]
    after = [{"urls": "turn:a:3478", "username": "u2", "credential": "c"}]
    assert _ice_fingerprint(before) != _ice_fingerprint(after)


def test_ice_fingerprint_differs_on_added_and_removed_entries():
    one = [dict(_TURN)]
    two = [dict(_TURN), dict(_TURN_2)]
    assert _ice_fingerprint(one) != _ice_fingerprint(two)
    assert _ice_fingerprint(two) != _ice_fingerprint([dict(_TURN_2)])


def test_ice_fingerprint_ignores_ordering():
    """Entry order and url order within an entry are irrelevant to
    connectivity, so a reshuffled list must not churn peers.
    """
    one = [
        {"urls": ["turn:a:3478", "turn:b:3478"], "username": "u", "credential": "c"},
        dict(_TURN_2),
    ]
    other = [
        dict(_TURN_2),
        {"urls": ["turn:b:3478", "turn:a:3478"], "username": "u", "credential": "c"},
    ]
    assert _ice_fingerprint(one) == _ice_fingerprint(other)
    assert _config_key(one) == _config_key(other)


def test_ice_fingerprint_none_and_empty_are_both_empty():
    assert _ice_fingerprint([]) == _ice_fingerprint(None)  # type: ignore[arg-type]
    assert _ice_fingerprint([]) == frozenset()


def test_ice_fingerprint_never_equates_configs_that_differ():
    """The load-bearing direction: if two lists would hand libdatachannel
    different ICE servers, they must NOT fingerprint equal — otherwise a
    real TURN update is silently swallowed and peers stay STUN-only.
    """
    variants = [
        [],
        [dict(_TURN)],
        [dict(_TURN_2)],
        [dict(_TURN), dict(_TURN_2)],
        [{**_TURN, "credential": "rotated"}],
        [{**_TURN, "username": "other"}],
        [{**_TURN, "urls": ["turn:relay.example:3478", "turn:relay.example:5349"]}],
        [{"urls": "stun:stun.example:3478"}],
    ]
    for i, a in enumerate(variants):
        for b in variants[i + 1 :]:
            assert _config_key(a) != _config_key(b), (a, b)
            assert _ice_fingerprint(a) != _ice_fingerprint(b), (a, b)


# ─── ICE-prime gate ────────────────────────────────────────────────────────


def _gated_transport(
    signal: _FakeSignaler,
    *,
    timeout_s: float = 5.0,
) -> FederationTransport:
    """A transport whose first handshake is gated on the ICE list being
    primed, with a test-sized timeout so the suite never waits out the
    production 15 s bound.
    """
    return FederationTransport(
        own_instance_id="self-iid",
        https_inbox=_RecordingHttpsInbox(),
        signaling_send=signal,
        ice_prime_timeout_s=timeout_s,
    )


async def test_ensure_handshake_waits_for_ice_primed():
    """REGRESSION (haos boot): the outbox drain used to build peers
    before ``HaIceServerSync`` delivered the Cloudflare TURN
    credentials, so the first OFFER went out STUN-only and the peer had
    to be retired and rebuilt. The first handshake now parks until the
    ICE list is primed.
    """
    signal = _FakeSignaler()
    t = _gated_transport(signal)
    inst = _fake_instance("peer-gated")

    task = asyncio.create_task(t.send(instance=inst, envelope_dict={"msg_id": "1"}))
    await asyncio.sleep(0.05)
    assert _offers(signal) == [], "OFFER went out before the ICE list was primed"
    assert inst.id not in t._peers

    t.mark_ice_primed()
    await task

    assert len(_offers(signal)) == 1
    await t.close_all()


async def test_ice_prime_timeout_latches_so_only_one_peer_pays_it():
    """The timeout sets the event as a latch: a platform that never
    primes (standalone, or an HA Core that never answers) costs the
    bound ONCE per process, not once per peer. Safe only because the
    ICE-generation/retire mechanism still carries a later list to every
    peer.
    """
    signal = _FakeSignaler()
    t = _gated_transport(signal, timeout_s=0.3)

    started = time.monotonic()
    await t.send(instance=_fake_instance("peer-a"), envelope_dict={"msg_id": "1"})
    await t.send(instance=_fake_instance("peer-b"), envelope_dict={"msg_id": "2"})
    elapsed = time.monotonic() - started

    assert len(_offers(signal)) == 2
    assert t._ice_primed.is_set()
    # One timeout, not two — generous margin for a loaded CI box.
    assert 0.3 <= elapsed < 0.6, elapsed
    await t.close_all()


async def test_mark_ice_primed_is_idempotent():
    """Safe before or after any handshake, and repeat calls never
    re-close the gate on a peer that already passed it.
    """
    signal = _FakeSignaler()
    t = _gated_transport(signal, timeout_s=30.0)
    t.mark_ice_primed()
    t.mark_ice_primed()
    assert t._ice_primed.is_set()

    await asyncio.wait_for(
        t.send(instance=_fake_instance("peer-idem"), envelope_dict={"msg_id": "1"}),
        timeout=1.0,
    )
    t.mark_ice_primed()
    assert t._ice_primed.is_set()
    assert len(_offers(signal)) == 1
    await t.close_all()


async def test_set_ice_servers_marks_primed():
    """Any push means the list is as good as it is going to get — the
    unchanged-list path returns early, so it has to release the gate
    before that return or a re-push of identical credentials would
    leave every handshake parked.
    """
    signal = _FakeSignaler()
    changed = _gated_transport(signal, timeout_s=30.0)
    changed.set_ice_servers([dict(_TURN)])
    assert changed._ice_primed.is_set()

    unchanged = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=_RecordingHttpsInbox(),
        signaling_send=signal,
        ice_servers=[dict(_TURN)],
        ice_prime_timeout_s=30.0,
    )
    generation = unchanged._ice_generation
    unchanged.set_ice_servers([dict(_TURN)])
    assert unchanged._ice_generation == generation, "unchanged list bumped generation"
    assert unchanged._ice_primed.is_set()

    await asyncio.wait_for(
        unchanged.send(
            instance=_fake_instance("peer-unchanged"),
            envelope_dict={"msg_id": "1"},
        ),
        timeout=1.0,
    )
    assert len(_offers(signal)) == 1
    await unchanged.close_all()


async def test_on_rtc_offer_does_not_wait_for_prime():
    """We are the answerer: a remote is blocked waiting on our ANSWER,
    and the lazy ``ice_provider`` already hands that PeerConnection the
    current list. Gating here would only add latency.
    """
    signal = _FakeSignaler()
    t = _gated_transport(signal, timeout_s=30.0)

    await asyncio.wait_for(
        t.on_rtc_offer(
            from_instance="peer-answerer",
            payload={"sdp": "v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\n", "sdp_type": "offer"},
        ),
        timeout=1.0,
    )

    assert not t._ice_primed.is_set()
    assert any(
        ev[1] is FederationEventType.FEDERATION_RTC_ANSWER for ev in signal.events
    )
    await t.close_all()


async def test_prime_wait_does_not_hold_the_transport_lock():
    """REGRESSION GUARD: the prime wait happens BEFORE ``self._lock`` is
    taken. Waiting inside it would block ``on_rtc_offer`` /
    ``on_rtc_ice`` / ``_retire_peer`` for every other peer for the whole
    timeout.
    """
    signal = _FakeSignaler()
    t = _gated_transport(signal, timeout_s=30.0)

    parked = asyncio.create_task(
        t.send(instance=_fake_instance("peer-parked"), envelope_dict={"msg_id": "1"}),
    )
    await asyncio.sleep(0.05)
    assert _offers(signal) == []

    # Another instance's ICE arrives: it takes ``self._lock`` to register
    # a buffering stub peer. An empty candidate short-circuits
    # ``add_ice_candidate`` so the only thing timed here is the lock.
    await asyncio.wait_for(
        t.on_rtc_ice(from_instance="peer-other", payload={"candidate": ""}),
        timeout=1.0,
    )
    assert "peer-other" in t._peers

    t.mark_ice_primed()
    await parked
    await t.close_all()


# ─── Dropped-candidate accounting + handshake restart (§24.12.5) ──────────
#
# REGRESSION (add-on log, peer z7k63…): the peer's outbox to us was
# congested, so its ANSWER arrived long after the 30 s candidate buffer
# had expired. Every trickled candidate was dropped permanently, the
# PeerConnection had zero remote candidates when the ANSWER finally
# landed, ICE could only fail, and the 24 h post-FAILED suppression then
# locked WebRTC out for the rest of the day. Raising the buffer timeout
# only moves the cliff — the fix is making the drop recoverable.


async def test_dropped_candidate_increments_counter(monkeypatch):
    """A candidate that times out waiting for the remote description is
    counted, not just logged — the count is what later makes the peer
    rebuildable."""
    monkeypatch.setattr(transport_mod, "ICE_BUFFER_TIMEOUT_S", 0.01)

    peer, _ = await _make_peer("aaaa", "bbbb")
    assert peer._dropped_candidates == 0

    await peer.add_ice_candidate(candidate="candidate:1 udp", sdp_mid="0")

    assert peer._dropped_candidates == 1
    # No recovery from here: the ANSWER may still be legitimately in
    # flight, and restarting mid-flight would thrash.
    assert peer._pc is None
    assert peer._restarts == 0
    # No PeerConnection existed to charge the drop to, so the peer is
    # waiting on an OFFER that may never come — the rebuild signal.
    assert peer._starved_awaiting_offer is True
    peer.close()


async def test_peer_awaiting_a_fresh_offer_reports_needs_rehandshake():
    """Second rebuild trigger: a peer sitting without a usable
    PeerConnection waiting for the remote to (re-)offer, whatever its
    ICE generation. Deliberately the flag and not the drop count — the
    count belongs to one PeerConnection and stays set on a connection
    that is negotiating perfectly well."""
    peer, _ = await _make_peer("aaaa", "bbbb")

    assert peer.needs_rehandshake(peer.ice_generation) is False
    peer._dropped_candidates = 1
    assert peer.needs_rehandshake(peer.ice_generation) is False
    peer._starved_awaiting_offer = True
    assert peer.needs_rehandshake(peer.ice_generation) is True

    # An open peer is still never rebuilt — it works.
    peer._open.set()
    assert peer.needs_rehandshake(peer.ice_generation) is False
    peer.close()


async def test_send_rebuilds_a_starved_stub_peer(monkeypatch):
    """REGRESSION (permanent black hole): ``on_rtc_ice`` creates a stub
    peer with no PeerConnection. If every buffered candidate is dropped
    and the OFFER never arrives, ``send()`` used to see ``peer is not
    None`` and skip the rebuild forever — HTTPS fallback for the life of
    the process. The drop count now retires and rebuilds it."""
    monkeypatch.setattr(transport_mod, "ICE_BUFFER_TIMEOUT_S", 0.01)

    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    t.mark_ice_primed()
    inst = _fake_instance("peer-starved")

    await t.on_rtc_ice(
        from_instance=inst.id,
        payload={"candidate": "candidate:1 udp", "sdp_mid": "0"},
    )
    stub = t._peers[inst.id]
    assert stub._dropped_candidates == 1
    assert stub._starved_awaiting_offer is True
    assert stub._pc is None
    signal.events.clear()

    await t.send(instance=inst, envelope_dict={"msg_id": "1"})

    assert len(_offers(signal)) == 1, "starved stub peer must be rebuilt"
    rebuilt = t._peers[inst.id]
    assert rebuilt is not stub
    assert stub._closed is True
    assert rebuilt._dropped_candidates == 0
    assert rebuilt._starved_awaiting_offer is False
    await t.close_all()


async def test_slow_answer_after_dropped_candidates_restarts_the_handshake(
    monkeypatch,
):
    """Offerer side: when the late ANSWER lands on a PC whose candidates
    were thrown away, re-offer instead of letting ICE fail."""
    monkeypatch.setattr(transport_mod, "ICE_BUFFER_TIMEOUT_S", 0.01)

    signal = _FakeSignaler()
    peer, _ = await _make_peer("aaaa", "bbbb", signal=signal)

    await peer.start_offer()
    first_pc = peer._pc
    assert len(_offers(signal)) == 1

    # Their candidates outran their ANSWER and were dropped.
    await peer.add_ice_candidate(candidate="candidate:1 udp", sdp_mid="0")
    assert peer._dropped_candidates == 1

    ok = await peer.apply_answer(sdp=_STUB_SDP, from_instance="bbbb")

    assert ok is True
    assert len(_offers(signal)) == 2, "a second OFFER must go out"
    assert peer._pc is not first_pc, "restart must build a fresh PC"
    assert peer._dropped_candidates == 0
    assert peer._restarts == 1
    peer.close()


async def test_answerer_with_dropped_candidates_closes_without_reoffering(
    monkeypatch,
):
    """Answerer side must NOT re-offer — that would manufacture glare.
    It tears the starved PC down and lets the offerer drive."""
    monkeypatch.setattr(transport_mod, "ICE_BUFFER_TIMEOUT_S", 0.01)

    signal = _FakeSignaler()
    peer, _ = await _make_peer("bbbb", "aaaa", signal=signal)

    await peer.add_ice_candidate(candidate="candidate:1 udp", sdp_mid="0")
    assert peer._dropped_candidates == 1

    await peer.accept_offer(sdp=_STUB_SDP, from_instance="aaaa")

    assert _offers(signal) == [], "answerer must never re-offer"
    answers = [
        e for e in signal.events if e[1] is FederationEventType.FEDERATION_RTC_ANSWER
    ]
    assert answers, "the ANSWER still goes out before the teardown"
    assert peer._pc is None, "the starved PC is torn down"
    # The teardown spends a restart credit so a remote that keeps
    # re-offering into a starved answerer cannot ping-pong forever.
    assert peer._restarts == 1
    # The teardown raises ``_starved_awaiting_offer``, so
    # ``needs_rehandshake`` stays true: if that remote never re-offers,
    # our own next ``send()`` rebuilds us as the offerer instead of
    # stranding us on HTTPS. The drop count, by contrast, is scoped to
    # the PeerConnection we just tore down and is already zero.
    assert peer._dropped_candidates == 0
    assert peer._starved_awaiting_offer is True
    assert peer.needs_rehandshake(0) is True
    peer.close()


async def test_answerer_teardown_is_bounded(monkeypatch):
    """A remote that keeps re-offering into a starved answerer cannot
    make us tear down forever — the teardown shares the offerer's
    ``MAX_HANDSHAKE_RESTARTS`` budget, after which the PC is left alone
    so ``_evict_peer``'s backoff owns the retry."""
    monkeypatch.setattr(transport_mod, "ICE_BUFFER_TIMEOUT_S", 0.01)
    signal = _FakeSignaler()
    peer, _ = await _make_peer("bbbb", "aaaa", signal=signal)
    for _ in range(transport_mod.MAX_HANDSHAKE_RESTARTS):
        await peer.add_ice_candidate(candidate="candidate:1 udp", sdp_mid="0")
        await peer.accept_offer(sdp=_STUB_SDP, from_instance="aaaa")
        assert peer._pc is None
    assert peer._restarts == transport_mod.MAX_HANDSHAKE_RESTARTS

    # Budget spent: the next starved accept keeps its PeerConnection.
    await peer.add_ice_candidate(candidate="candidate:1 udp", sdp_mid="0")
    await peer.accept_offer(sdp=_STUB_SDP, from_instance="aaaa")
    assert peer._pc is not None, "budget spent — PC left to reach FAILED"
    assert _offers(signal) == [], "answerer must never re-offer"
    peer.close()


async def test_start_offer_clears_drops_from_the_replaced_connection():
    """A fresh OFFER starts a clean negotiation: drops charged to the
    PeerConnection being replaced must not re-trigger recovery against
    the new one (that would loop)."""
    peer, _ = await _make_peer("bbbb", "aaaa")
    peer._dropped_candidates = 3
    peer._starved_awaiting_offer = True
    await peer.start_offer()
    assert peer._dropped_candidates == 0
    assert peer._starved_awaiting_offer is False, "we are the offerer now"
    assert peer.needs_rehandshake(0) is False
    peer.close()


async def test_restart_is_bounded_by_max_handshake_restarts(monkeypatch):
    """No new hammering: a persistently-late peer restarts at most
    ``MAX_HANDSHAKE_RESTARTS`` times, then the PC is left to reach FAILED
    so ``_evict_peer``'s backoff takes over."""
    monkeypatch.setattr(transport_mod, "ICE_BUFFER_TIMEOUT_S", 0.01)

    signal = _FakeSignaler()
    peer, _ = await _make_peer("aaaa", "bbbb", signal=signal)

    await peer.start_offer()
    for _ in range(MAX_HANDSHAKE_RESTARTS + 2):
        peer._dropped_candidates = 1
        await peer.apply_answer(sdp=_STUB_SDP, from_instance="bbbb")

    assert peer._restarts == MAX_HANDSHAKE_RESTARTS
    # One initial offer plus one per restart — and no more.
    assert len(_offers(signal)) == MAX_HANDSHAKE_RESTARTS + 1
    peer.close()


async def test_restart_does_not_fire_when_no_candidates_dropped():
    """Happy path stays churn-free: a normal ANSWER never re-offers."""
    signal = _FakeSignaler()
    peer, _ = await _make_peer("aaaa", "bbbb", signal=signal)

    await peer.start_offer()
    first_pc = peer._pc

    ok = await peer.apply_answer(sdp=_STUB_SDP, from_instance="bbbb")

    assert ok is True
    assert len(_offers(signal)) == 1
    assert peer._pc is first_pc
    assert peer._restarts == 0
    peer.close()


async def test_restarted_peer_does_not_restart_again(monkeypatch):
    """The restarted peer starts from a clean drop count, so neither a
    follow-up ANSWER nor ``needs_rehandshake`` re-triggers recovery."""
    monkeypatch.setattr(transport_mod, "ICE_BUFFER_TIMEOUT_S", 0.01)

    signal = _FakeSignaler()
    peer, _ = await _make_peer("aaaa", "bbbb", signal=signal)

    await peer.start_offer()
    await peer.add_ice_candidate(candidate="candidate:1 udp", sdp_mid="0")
    await peer.apply_answer(sdp=_STUB_SDP, from_instance="bbbb")
    restarted_pc = peer._pc
    assert len(_offers(signal)) == 2

    assert peer.needs_rehandshake(peer.ice_generation) is False
    await peer.apply_answer(sdp=_STUB_SDP, from_instance="bbbb")

    assert len(_offers(signal)) == 2, "no second restart without a new drop"
    assert peer._pc is restarted_pc
    assert peer._restarts == 1
    peer.close()


async def test_restart_from_apply_answer_does_not_cancel_its_own_task(
    monkeypatch,
):
    """``_close_pc()`` cancels every task spawned via ``pc.spawn_task``.
    ``apply_answer`` runs on the inbound-signalling coroutine (from
    ``on_rtc_answer``), never on a spawned task, so the restart cannot
    cancel the coroutine that triggered it. This pins that: the caller
    must run to completion."""
    monkeypatch.setattr(transport_mod, "ICE_BUFFER_TIMEOUT_S", 0.01)

    https_inbox = _RecordingHttpsInbox()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=signal,
    )
    t.mark_ice_primed()
    inst = _fake_instance("peer-selfcancel")
    await t._ensure_handshake(inst)
    peer = t._peers[inst.id]
    await peer.add_ice_candidate(candidate="candidate:1 udp", sdp_mid="0")

    finished = False

    async def _inbound_signalling() -> None:
        nonlocal finished
        await t.on_rtc_answer(
            from_instance=inst.id,
            payload={"sdp": _STUB_SDP, "sdp_type": "answer"},
        )
        finished = True

    task = asyncio.create_task(_inbound_signalling())
    await task

    assert finished is True
    assert task.cancelled() is False
    assert peer._restarts == 1
    await t.close_all()


# ─── Drop accounting is scoped to the PeerConnection ──────────────────────
#
# REGRESSION (reviewer repro): ``_dropped_candidates`` used to be a
# permanent property of the peer OBJECT — only ``start_offer`` ever
# zeroed it. One candidate outrunning its OFFER by more than
# ``ICE_BUFFER_TIMEOUT_S`` therefore made the answerer tear down the
# next TWO PeerConnections (each one built, answered, then immediately
# closed while the offerer believed the handshake had landed), and left
# ``needs_rehandshake`` permanently true afterwards — so ``send()``
# retired a healthy, still-negotiating peer every outbox poll.


async def test_one_transient_drop_does_not_poison_later_clean_offers(monkeypatch):
    """One drop buys exactly ONE recovery teardown. The offer that
    follows is clean — the drop belonged to the connection we already
    tore down — so its PeerConnection must survive and the peer must
    not report ``needs_rehandshake``."""
    monkeypatch.setattr(transport_mod, "ICE_BUFFER_TIMEOUT_S", 0.01)

    signal = _FakeSignaler()
    peer, _ = await _make_peer("bbbb", "aaaa", signal=signal)

    await peer.add_ice_candidate(candidate="candidate:1 udp", sdp_mid="0")
    assert peer._dropped_candidates == 1

    # Genuinely starved: this OFFER is the remote description the dropped
    # candidate was waiting for, so the fresh PC really is short a
    # candidate. One teardown, and we wait for the offerer to re-offer.
    await peer.accept_offer(sdp=_STUB_SDP, from_instance="aaaa")
    assert peer._pc is None
    assert peer._restarts == 1
    assert peer._starved_awaiting_offer is True

    # The re-offer lands with nothing dropped against it.
    await peer.accept_offer(sdp=_STUB_SDP, from_instance="aaaa")

    assert peer._pc is not None, "a clean PC must not be torn down"
    assert peer._dropped_candidates == 0
    assert peer._starved_awaiting_offer is False
    assert peer.needs_rehandshake(peer.ice_generation) is False
    assert peer._restarts == 1, "no restart credit spent on a clean offer"
    assert _offers(signal) == [], "answerer must never re-offer"
    peer.close()


async def test_two_clean_offers_after_one_drop_keep_their_pc(monkeypatch):
    """The stale-drop seam bit twice (``MAX_HANDSHAKE_RESTARTS``), so a
    single-teardown assertion would not have caught it. Two consecutive
    clean offers after the one recovery teardown must both keep their
    PeerConnection."""
    monkeypatch.setattr(transport_mod, "ICE_BUFFER_TIMEOUT_S", 0.01)

    signal = _FakeSignaler()
    peer, _ = await _make_peer("bbbb", "aaaa", signal=signal)

    await peer.add_ice_candidate(candidate="candidate:1 udp", sdp_mid="0")
    await peer.accept_offer(sdp=_STUB_SDP, from_instance="aaaa")
    assert peer._pc is None, "the starved PC is torn down once"

    for attempt in (1, 2):
        await peer.accept_offer(sdp=_STUB_SDP, from_instance="aaaa")
        assert peer._pc is not None, f"clean offer {attempt} was torn down"
        assert peer._restarts == 1, f"clean offer {attempt} spent a restart"
        assert peer.needs_rehandshake(peer.ice_generation) is False

    assert _offers(signal) == []
    peer.close()


async def test_torn_down_answerer_rebuilds_as_offerer_on_next_send(monkeypatch):
    """The intent behind the answerer teardown: if the remote never
    re-offers (an older peer still suppressing rebuilds for a flat 24 h,
    or one deep in its graduated backoff), our own next ``send()`` must
    rebuild us as the OFFERER rather than stranding the peer on HTTPS.
    Carried by ``_starved_awaiting_offer`` now that the drop count is
    scoped to the PeerConnection."""
    monkeypatch.setattr(transport_mod, "ICE_BUFFER_TIMEOUT_S", 0.01)

    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=_RecordingHttpsInbox(),
        signaling_send=signal,
    )
    t.mark_ice_primed()
    inst = _fake_instance("peer-answerer")

    await t.on_rtc_ice(
        from_instance=inst.id,
        payload={"candidate": "candidate:1 udp", "sdp_mid": "0"},
    )
    peer = t._peers[inst.id]
    await t.on_rtc_offer(
        from_instance=inst.id,
        payload={"sdp": _STUB_SDP, "sdp_type": "offer"},
    )
    assert peer._pc is None, "starved answerer PC is torn down"
    assert peer._starved_awaiting_offer is True
    assert peer.needs_rehandshake(t._ice_generation) is True
    signal.events.clear()

    await t.send(instance=inst, envelope_dict={"msg_id": "1"})

    assert len(_offers(signal)) == 1, "we must re-offer instead of staying HTTPS-only"
    rebuilt = t._peers[inst.id]
    assert rebuilt is not peer
    assert rebuilt._starved_awaiting_offer is False
    await t.close_all()


# ─── Retire rate floor ────────────────────────────────────────────────────


async def test_retire_rate_floor_skips_a_rapid_second_rebuild():
    """A peer that trickles ICE while its ANSWER never lands flips
    ``needs_rehandshake`` roughly every ``ICE_BUFFER_TIMEOUT_S``, and
    each retire mints a PeerConnection (ICE gathering + a TURN
    allocation) and POSTs an OFFER. ``peer.close()`` from the retire
    path pre-empts the FAILED → ``_evict_peer`` route, so nothing else
    throttles it. A per-instance floor caps it without stamping a
    suppression."""
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=_RecordingHttpsInbox(),
        signaling_send=signal,
        rtc_retry_backoff_base_s=60.0,
    )
    t.mark_ice_primed()
    inst = _fake_instance("peer-floor")
    old = _stub_peer(t, inst.id)
    t.set_ice_servers([_TURN])
    signal.events.clear()

    await t.send(instance=inst, envelope_dict={"msg_id": "1"})
    assert len(_offers(signal)) == 1
    rebuilt = t._peers[inst.id]
    assert rebuilt is not old

    # The rebuilt peer starves again immediately (candidates keep
    # trickling, the ANSWER never lands).
    rebuilt._starved_awaiting_offer = True

    result = await t.send(instance=inst, envelope_dict={"msg_id": "2"})

    assert len(_offers(signal)) == 1, "second retire inside the floor must be skipped"
    assert t._peers[inst.id] is rebuilt, "a skipped retire leaves the peer in place"
    assert rebuilt._closed is False
    assert result.via == "https", "the skipped retire falls through to HTTPS"
    # A floor is not a backoff: no suppression, no failure count.
    assert inst.id not in t._rtc_suppressed_until
    assert inst.id not in t._rtc_failure_count
    await t.close_all()


async def test_retire_rate_floor_allows_the_rebuild_once_it_elapses():
    """The floor only caps the pathological loop — the real case (a
    generation bump, which happens once) must still rebuild, and so must
    a genuine later retire."""
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=_RecordingHttpsInbox(),
        signaling_send=signal,
        rtc_retry_backoff_base_s=60.0,
    )
    t.mark_ice_primed()
    inst = _fake_instance("peer-floor-expiry")
    _stub_peer(t, inst.id)
    t.set_ice_servers([_TURN])
    signal.events.clear()

    await t.send(instance=inst, envelope_dict={"msg_id": "1"})
    rebuilt = t._peers[inst.id]
    assert len(_offers(signal)) == 1

    # Back-date the stamp past the floor rather than sleeping it out.
    t._last_retire_at[inst.id] = time.monotonic() - 120.0
    rebuilt._starved_awaiting_offer = True

    await t.send(instance=inst, envelope_dict={"msg_id": "2"})

    assert len(_offers(signal)) == 2, "retire must be allowed once the floor elapsed"
    assert t._peers[inst.id] is not rebuilt
    assert rebuilt._closed is True
    await t.close_all()


async def test_peer_open_prunes_the_retire_stamp():
    """A healthy peer must not carry stale retire state: its next
    genuine retire (say a TURN list lands months later) is rate-limited
    against a timestamp from a different era otherwise."""
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=_RecordingHttpsInbox(),
        signaling_send=signal,
    )
    t._last_retire_at["peer-open"] = time.monotonic()

    await t._on_peer_open("peer-open")

    assert "peer-open" not in t._last_retire_at


# ─── Shutdown: no resurrection through the prime gate ─────────────────────


async def test_close_all_during_prime_wait_builds_no_peer():
    """REGRESSION: ``_ensure_handshake`` awaits the ICE-primed gate
    BEFORE taking the lock and inserting into ``_peers``. A ``send()``
    parked there across ``on_cleanup`` used to resume afterwards, insert
    a peer, build a PeerConnection and POST an OFFER during shutdown —
    and nothing ever closed it."""
    signal = _FakeSignaler()
    t = _gated_transport(signal, timeout_s=30.0)
    inst = _fake_instance("peer-shutdown")

    task = asyncio.create_task(t.send(instance=inst, envelope_dict={"msg_id": "1"}))
    await asyncio.sleep(0.05)
    assert inst.id not in t._peers

    await t.close_all()
    # The gate releases after shutdown (a late platform push, or the
    # bounded timeout latching it open).
    t.mark_ice_primed()
    await task

    assert t._peers == {}, "shutdown must not resurrect a peer"
    assert _offers(signal) == [], "no OFFER may go out during shutdown"


async def test_close_all_racing_start_offer_leaves_no_open_pc():
    """The narrower race: the peer was inserted and ``start_offer`` is
    already in flight when ``close_all`` runs. The lock is released
    across that await, so the flag has to be re-checked afterwards or
    the PeerConnection leaks past shutdown."""
    signal = _FakeSignaler()
    t = _gated_transport(signal, timeout_s=30.0)
    t.mark_ice_primed()
    inst = _fake_instance("peer-race")

    caught: list = []

    async def _signal_and_close(to_instance_id, event_type, payload):
        signal.events.append((to_instance_id, event_type, payload))
        if event_type is FederationEventType.FEDERATION_RTC_OFFER:
            caught.append(t._peers[to_instance_id])
            await t.close_all()
        return DeliveryResult(instance_id=to_instance_id, ok=True, status_code=200)

    t._signaling_send = _signal_and_close

    await t.send(instance=inst, envelope_dict={"msg_id": "1"})

    assert caught, "the OFFER must have gone out while the peer was registered"
    assert t._peers == {}, "the peer built across the race must be dropped"
    assert caught[0]._closed is True, "the PeerConnection leaked past shutdown"
    assert caught[0]._pc is None


async def test_closing_flag_raised_mid_offer_drops_the_peer():
    """The re-check after the ``start_offer()`` await, in the shape where
    the peer is still registered when it runs: the transport latched shut
    while the OFFER was on the wire, so the peer we just built has to be
    removed and closed rather than left behind for the process lifetime.
    """
    signal = _FakeSignaler()
    t = _gated_transport(signal, timeout_s=30.0)
    t.mark_ice_primed()
    inst = _fake_instance("peer-latched")

    caught: list = []

    async def _signal_and_latch(to_instance_id, event_type, payload):
        signal.events.append((to_instance_id, event_type, payload))
        if event_type is FederationEventType.FEDERATION_RTC_OFFER:
            # ``close_all()`` drains ``_peers`` itself, so raise the flag
            # alone to pin the identity-scoped cleanup underneath it.
            caught.append(t._peers[to_instance_id])
            t._closing = True
        return DeliveryResult(instance_id=to_instance_id, ok=True, status_code=200)

    t._signaling_send = _signal_and_latch

    await t.send(instance=inst, envelope_dict={"msg_id": "1"})

    assert caught, "the peer must have been registered while offering"
    assert inst.id not in t._peers
    assert caught[0]._closed is True
    assert len(_offers(signal)) == 1


# ─── Transport selection: the connection-server relay tier ────────────────


class _RecordingRelay:
    """Drop-in :class:`GfsRelayTransport` for facade selection tests."""

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[RemoteInstance, dict]] = []

    async def send(self, *, instance, envelope_dict):
        self.calls.append((instance, envelope_dict))
        return self.ok, None


def _link_joined_instance(iid: str = "link-peer") -> RemoteInstance:
    """A household seated from an invite link: no address, ever."""
    return RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="bb" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url="",
        local_inbox_id=f"wh-{iid}",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.SPACE_SESSION,
        relay_via="https://gfs.example.org",
        remote_keywrap_pk="cc" * 32,
    )


async def test_space_session_peer_goes_to_the_relay_never_https():
    """A link-joined household has NO inbox URL by design. Letting it
    fall through to the HTTPS inbox would POST at the empty string on
    every space event; it rides the connection-server relay instead."""
    https_inbox = _RecordingHttpsInbox()
    relay = _RecordingRelay()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        gfs_relay=relay,
        signaling_send=_FakeSignaler(),
    )

    result = await t.send(
        instance=_link_joined_instance(),
        envelope_dict={"msg_id": "m-1", "event_type": "space.post_created"},
    )

    assert result.ok is True
    assert result.via == "gfs_relay"
    assert [c[1]["msg_id"] for c in relay.calls] == ["m-1"]
    # The HTTPS inbox was never touched, and no RTC handshake started.
    assert https_inbox.calls == []
    assert t._peers == {}


async def test_confirmed_peer_with_an_address_is_unchanged_by_the_relay_tier():
    """The selection change must not touch the ordinary path: a normal
    peer still tries RTC and falls back to its HTTPS inbox."""
    https_inbox = _RecordingHttpsInbox()
    relay = _RecordingRelay()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        gfs_relay=relay,
        signaling_send=_FakeSignaler(),
    )

    result = await t.send(
        instance=_fake_instance("peer-1"),
        envelope_dict={"msg_id": "m-2"},
    )

    assert result.via == "https"
    assert result.ok is True
    assert relay.calls == []
    assert [c[1]["msg_id"] for c in https_inbox.calls] == ["m-2"]


async def test_space_session_peer_fails_closed_without_a_relay():
    """No relay wired → a named failure, still never an HTTPS POST at ''."""
    https_inbox = _RecordingHttpsInbox()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        signaling_send=_FakeSignaler(),
    )

    result = await t.send(
        instance=_link_joined_instance(),
        envelope_dict={"msg_id": "m-3"},
    )

    assert result.ok is False
    assert result.error == "gfs_relay_unavailable"
    assert https_inbox.calls == []


async def test_relay_failure_is_a_transport_failure_not_a_raise():
    https_inbox = _RecordingHttpsInbox()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https_inbox,
        gfs_relay=_RecordingRelay(ok=False),
        signaling_send=_FakeSignaler(),
    )

    result = await t.send(
        instance=_link_joined_instance(),
        envelope_dict={"msg_id": "m-4"},
    )

    assert result.ok is False
    assert result.via == "gfs_relay"
    assert result.error == "gfs_relay_failed"
    assert https_inbox.calls == []
