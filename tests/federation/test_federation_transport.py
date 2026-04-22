"""Tests for FederationTransport (§24.12.5).

The test conftest injects a fake ``aiolibdatachannel`` module into
``sys.modules`` before any production imports, so the DataChannel
state machine uses deterministic fake objects. The peer ``is_ready``
flag is flipped explicitly by marking ``_open`` / ``_closed`` on
``_RtcPeer``. That's enough to exercise the facade's primary /
fallback branches without the native binding.
"""

from __future__ import annotations


from socialhome.domain.federation import (
    DeliveryResult,
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.transport import (
    FederationTransport,
    WebhookTransport,
    _RtcPeer,
)


# ─── Fakes ────────────────────────────────────────────────────────────────


def _fake_instance(iid: str = "peer-1") -> RemoteInstance:
    return RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_webhook_url="https://peer/wh",
        local_webhook_id=f"wh-{iid}",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )


class _RecordingWebhook:
    """Drop-in replacement for :class:`WebhookTransport` used by facade tests."""

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


async def test_send_uses_rtc_when_peer_is_ready():
    """A peer whose DataChannel is already open takes the RTC path."""
    webhook = _RecordingWebhook()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        webhook=webhook,
        signaling_send=signal,
    )
    inst = _fake_instance("peer-1")

    # Synthesise a ready peer (stub mode never opens the channel
    # on its own).
    peer = _RtcPeer(
        instance_id=inst.id,
        ice_servers=None,
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
    assert webhook.calls == []  # no webhook fallback


async def test_send_falls_back_to_webhook_when_peer_not_ready():
    """No RTC channel yet → facade starts a handshake AND uses webhook."""
    webhook = _RecordingWebhook()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        webhook=webhook,
        signaling_send=signal,
    )
    inst = _fake_instance("peer-2")

    result = await t.send(instance=inst, envelope_dict={"msg_id": "x"})

    assert result.ok is True
    assert result.via == "webhook"
    assert len(webhook.calls) == 1
    # Handshake was kicked — one OFFER was sent through the signaler.
    assert (
        signal.events
        and signal.events[0][1] is FederationEventType.FEDERATION_RTC_OFFER
    )


async def test_send_falls_back_when_webhook_fails():
    """Webhook returning non-2xx bubbles up as ``ok=False``."""
    webhook = _RecordingWebhook(ok=False, status=502)
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        webhook=webhook,
        signaling_send=signal,
    )
    result = await t.send(
        instance=_fake_instance("peer-3"),
        envelope_dict={"msg_id": "x"},
    )
    assert result.ok is False
    assert result.via == "webhook"
    assert result.status_code == 502


async def test_send_falls_back_to_webhook_when_rtc_send_raises():
    """An RTC send that errors is swallowed; webhook delivers instead."""
    webhook = _RecordingWebhook()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        webhook=webhook,
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
        ice_servers=None,
        signaling=t._signaling_factory(inst.id),
        inbound=t._inbound_factory(inst.id),
    )
    t._peers[inst.id] = peer

    result = await t.send(instance=inst, envelope_dict={"msg_id": "x"})
    assert result.ok is True
    assert result.via == "webhook"
    assert webhook.calls


# ─── Inbound signalling ────────────────────────────────────────────────────


async def test_on_rtc_offer_creates_peer_and_sends_answer():
    webhook = _RecordingWebhook()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        webhook=webhook,
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
    webhook = _RecordingWebhook()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        webhook=webhook,
        signaling_send=signal,
    )
    await t.on_rtc_offer(from_instance="peer-6", payload={"sdp": ""})
    # Peer was still registered (we hold the slot) but no ANSWER sent.
    assert not any(
        ev[1] is FederationEventType.FEDERATION_RTC_ANSWER for ev in signal.events
    )


async def test_on_rtc_answer_with_matching_from_applies():
    """S-14: the answer origin must match the pending-offer target."""
    webhook = _RecordingWebhook()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        webhook=webhook,
        signaling_send=signal,
    )
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
    webhook = _RecordingWebhook()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        webhook=webhook,
        signaling_send=signal,
    )
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
    webhook = _RecordingWebhook()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        webhook=webhook,
        signaling_send=signal,
    )
    await t.on_rtc_answer(
        from_instance="ghost",
        payload={"sdp": "x"},
    )
    assert t.peer_count() == 0


async def test_on_rtc_ice_unknown_peer_is_noop():
    webhook = _RecordingWebhook()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        webhook=webhook,
        signaling_send=signal,
    )
    # Should not raise.
    await t.on_rtc_ice(
        from_instance="ghost",
        payload={"candidate": "c", "sdp_mid": "0"},
    )


async def test_on_rtc_ice_accepts_trickled_candidate():
    webhook = _RecordingWebhook()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        webhook=webhook,
        signaling_send=signal,
    )
    inst = _fake_instance("peer-9")
    await t._ensure_handshake(inst)
    # Should not raise — stub peer accepts candidates into its list.
    await t.on_rtc_ice(
        from_instance="peer-9",
        payload={
            "candidate": "candidate:1 udp 1 1.1.1.1 5000 typ host",
            "sdp_mid": "0",
        },
    )


# ─── Facade lifecycle ──────────────────────────────────────────────────────


async def test_close_peer_removes_entry():
    webhook = _RecordingWebhook()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        webhook=webhook,
        signaling_send=signal,
    )
    await t._ensure_handshake(_fake_instance("peer-10"))
    assert t.peer_count() == 1
    await t.close_peer("peer-10")
    assert t.peer_count() == 0


async def test_close_all_drops_every_peer():
    webhook = _RecordingWebhook()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        webhook=webhook,
        signaling_send=signal,
    )
    await t._ensure_handshake(_fake_instance("a"))
    await t._ensure_handshake(_fake_instance("b"))
    await t.close_all()
    assert t.peer_count() == 0


async def test_is_ready_reports_false_for_unknown_peer():
    webhook = _RecordingWebhook()
    signal = _FakeSignaler()
    t = FederationTransport(
        own_instance_id="self-iid",
        webhook=webhook,
        signaling_send=signal,
    )
    assert t.is_ready("never-seen") is False


# ─── WebhookTransport ──────────────────────────────────────────────────────


async def test_webhook_transport_2xx_is_ok():
    """WebhookTransport.send returns (True, status) for 2xx."""

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

    wt = WebhookTransport(client_factory=_factory)
    ok, status = await wt.send(
        instance=_fake_instance("peer"),
        envelope_dict={"msg_id": "x"},
    )
    assert ok is True and status == 204


async def test_webhook_transport_non_2xx_is_failure():
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

    wt = WebhookTransport(client_factory=_factory)
    ok, status = await wt.send(
        instance=_fake_instance("peer"),
        envelope_dict={"x": 1},
    )
    assert ok is False and status == 503


async def test_webhook_transport_network_error_is_failure():
    class _RaisingClient:
        def post(self, *a, **kw):
            raise RuntimeError("boom")

    async def _factory():
        return _RaisingClient()

    wt = WebhookTransport(client_factory=_factory)
    ok, status = await wt.send(
        instance=_fake_instance("peer"),
        envelope_dict={"x": 1},
    )
    assert ok is False and status is None
