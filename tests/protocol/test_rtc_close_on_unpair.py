"""A pairing that ends closes its WebRTC PeerConnection (§11, §24.11).

When a pairing ends, :meth:`PeerUnpairService.purge` calls
:meth:`FederationTransport.close_peer`. It must:

* close the peer and drop it from the registry — no ICE/DTLS association
  lingers between ex-peers;
* let a frame still buffered on the DataChannel (our ``UNPAIR``) leave
  before the close, within a bound;
* never cut short the processing of the inbound frame that ended the
  pairing (the peer's ``UNPAIR`` over the DataChannel) — closing the
  PeerConnection cancels the channel's receive task it runs in;
* close the peer once, never twice;
* leave nothing behind that stops a later re-pair from building a fresh,
  working connection — including a peer whose channel the OTHER side
  closed (the ``_closed``-peer rebuild gap).

Runs on the conftest fake of ``aiolibdatachannel``; the real wire is
covered by the federation-demo ``unpair`` / ``unpair-offline`` steps.
"""

from __future__ import annotations

import asyncio

import orjson
import pytest

from socialhome.domain.federation import (
    DeliveryResult,
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation import transport as transport_module
from socialhome.federation.transport import FederationTransport

pytestmark = pytest.mark.security

PEER = "peer-unpaired"


def _instance(iid: str = PEER) -> RemoteInstance:
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


class _Inbox:
    async def send(self, *, instance, envelope_dict):
        return True, 200


class _Signals:
    def __init__(self) -> None:
        self.offers = 0

    async def __call__(self, to, event_type, payload):
        if event_type is FederationEventType.FEDERATION_RTC_OFFER:
            self.offers += 1
        return DeliveryResult(instance_id=to, ok=True, status_code=200)


def _transport(inbound_handler=None, signals: _Signals | None = None):
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=_Inbox(),  # type: ignore[arg-type]
        signaling_send=signals or _Signals(),
        inbound_handler=inbound_handler,
    )
    t.mark_ice_primed()
    return t


async def _open_peer(t: FederationTransport):
    await t._ensure_handshake(_instance())
    peer = t._peers[PEER]
    for _ in range(50):
        if peer.is_ready:
            return peer
        await asyncio.sleep(0)
    raise AssertionError("fake channel never opened")


async def test_close_peer_closes_and_removes_the_peer():
    t = _transport()
    peer = await _open_peer(t)
    pc = peer._pc

    await t.close_peer(PEER)

    assert t.peer_count() == 0
    assert peer._closed and peer._pc is None
    assert pc.closed


async def test_buffered_unpair_leaves_before_the_close():
    t = _transport()
    peer = await _open_peer(t)
    pc, channel = peer._pc, peer._channel
    # Our UNPAIR was accepted by ``send`` but still sits in the send buffer.
    channel.buffered_amount = 512

    closing = asyncio.create_task(t.close_peer(PEER))
    await asyncio.sleep(0.2)
    assert not pc.closed, "closed while the UNPAIR was still buffered"
    # No new traffic can pick the peer up meanwhile.
    assert t.peer_count() == 0

    channel.buffered_amount = 0
    await asyncio.wait_for(closing, 2)
    assert pc.closed


async def test_flush_wait_is_bounded(monkeypatch):
    monkeypatch.setattr(transport_module, "RTC_CLOSE_FLUSH_TIMEOUT_S", 0.2)
    t = _transport()
    peer = await _open_peer(t)
    pc = peer._pc
    peer._channel.buffered_amount = 512  # never drains (peer gone)

    await asyncio.wait_for(t.close_peer(PEER), 2)

    assert pc.closed


async def test_inbound_frame_that_ends_the_pairing_is_processed_to_the_end():
    finished: list[str] = []
    holder: dict = {}

    async def _handler(instance_id: str, raw: bytes) -> dict:
        envelope = orjson.loads(raw)
        # What ``PeerUnpairService.forget`` does with an inbound UNPAIR:
        # purge (which closes the peer this frame arrived on), then more
        # awaits (publish PeerUnpaired, its subscribers).
        await holder["t"].close_peer(instance_id)
        await asyncio.sleep(0.05)
        finished.append(envelope["event_type"])
        return {}

    t = _transport(inbound_handler=_handler)
    holder["t"] = t
    peer = await _open_peer(t)
    pc = peer._pc

    await peer._channel._inbox.put(orjson.dumps({"event_type": "unpair"}))

    for _ in range(100):
        if finished:
            break
        await asyncio.sleep(0.01)
    assert finished == ["unpair"]
    assert pc.closed
    assert t.peer_count() == 0


async def test_close_peer_closes_once(monkeypatch):
    t = _transport()
    peer = await _open_peer(t)
    pc = peer._pc
    closes: list[int] = []
    real_close = pc.close

    def _counting_close() -> None:
        closes.append(1)
        real_close()

    monkeypatch.setattr(pc, "close", _counting_close)

    await t.close_peer(PEER)
    await t.close_peer(PEER)  # the purge of a settled tombstone, say
    await t.close_all()

    assert closes == [1]


async def test_close_peer_unknown_is_noop():
    t = _transport()
    await t.close_peer("never-paired")
    assert t.peer_count() == 0


async def test_repair_after_close_builds_a_working_connection():
    """Unpair closes the peer; a later re-pair's first send dials a fresh
    PeerConnection whose DataChannel opens and carries the frame."""
    signals = _Signals()
    t = _transport(signals=signals)
    old = await _open_peer(t)
    await t.close_peer(PEER)
    offers_before = signals.offers

    await t.send(instance=_instance(), envelope_dict={"msg_id": "re-paired"})
    fresh = t._peers[PEER]
    for _ in range(50):
        if fresh.is_ready:
            break
        await asyncio.sleep(0)

    assert fresh is not old
    assert fresh.is_ready
    assert signals.offers == offers_before + 1
    sent = await t.send(instance=_instance(), envelope_dict={"msg_id": "over-rtc"})
    assert sent.ok and sent.via == "rtc"
    assert fresh._channel.sent[-1] == orjson.dumps({"msg_id": "over-rtc"})


async def test_peer_whose_channel_ended_is_rebuilt_on_next_send():
    """The other side closed the connection (its unpair) — our peer saw its
    channel end and set ``_closed``. A later re-pair must not leave us on
    the HTTPS inbox for good: the next send builds a fresh peer."""
    signals = _Signals()
    t = _transport(signals=signals)
    peer = await _open_peer(t)
    peer._channel.close()  # remote went away: the recv loop ends
    for _ in range(50):
        if peer._closed:
            break
        await asyncio.sleep(0)
    assert peer._closed and not peer.is_ready
    offers_before = signals.offers

    await t.send(instance=_instance(), envelope_dict={"msg_id": "again"})

    fresh = t._peers[PEER]
    assert fresh is not peer
    assert signals.offers == offers_before + 1
    assert peer._pc is None  # the spent peer was closed, not leaked


async def test_offer_onto_a_spent_peer_gets_a_fresh_peer():
    t = _transport()
    peer = await _open_peer(t)
    peer._channel.close()
    for _ in range(50):
        if peer._closed:
            break
        await asyncio.sleep(0)

    await t.on_rtc_offer(from_instance=PEER, payload={"sdp": "v=0\r\n"})

    fresh = t._peers[PEER]
    assert fresh is not peer
    assert not fresh._closed


async def test_ice_onto_a_spent_peer_gets_a_fresh_peer():
    t = _transport()
    peer = await _open_peer(t)
    peer._channel.close()
    for _ in range(50):
        if peer._closed:
            break
        await asyncio.sleep(0)

    # The candidate overtook the OFFER: the fresh stub parks it until the
    # OFFER lands, so do not wait for that.
    parked = asyncio.create_task(
        t.on_rtc_ice(
            from_instance=PEER,
            payload={"candidate": "candidate:1 1 UDP 1 127.0.0.1 9 typ host"},
        )
    )
    await asyncio.sleep(0.05)

    assert t._peers[PEER] is not peer
    await t.close_all()  # wakes the parked candidate
    await asyncio.wait_for(parked, 2)


async def test_failed_peer_is_left_to_its_eviction():
    """A FAILED peer is evicted with a backoff by ``_evict_peer``; the
    spent-peer rebuild must not jump that suppression."""
    t = _transport()
    peer = await _open_peer(t)
    peer._closed = True
    peer._failed = True

    assert not peer.needs_rehandshake(t._ice_generation)
