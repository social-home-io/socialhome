"""Tests for the binary media DataChannel (``fed-media-v1``) on the
federation transport — ``_RtcPeer`` second-channel lifecycle, framing
round-trip over the channel, and the facade ``send_media``.

The conftest fake ``aiolibdatachannel`` is injected before production
imports, so ``_RtcPeer`` builds against the stub PeerConnection.
"""

from __future__ import annotations

import aiolibdatachannel as rtc
import orjson
import pytest

from socialhome.domain.federation import RemoteInstance, PairingStatus
from socialhome.federation import app_framing as af
from socialhome.federation import media_framing as mf
from socialhome.federation.transport import FederationTransport, _RtcPeer

pytestmark = pytest.mark.asyncio


def _fake_instance(iid: str = "peer-1") -> RemoteInstance:
    return RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url="http://x/inbox",
        local_inbox_id="wh",
        status=PairingStatus.CONFIRMED,
    )


class _ScriptedChannel:
    """Deterministic channel: yields a fixed list of inbound messages
    then stops; records outbound sends; configurable backpressure."""

    def __init__(self, messages=None, *, label=mf.CHANNEL_LABEL):
        self.label = label
        self._messages = list(messages or [])
        self.sent: list = []
        self.buffered_amount = 0
        self.raise_on_send: Exception | None = None

    def set_buffered_amount_low_threshold(self, n: int) -> None:
        pass

    async def wait_open(self) -> None:
        pass

    async def send(self, data) -> None:
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        raise StopAsyncIteration


def _peer(media_inbound=None) -> _RtcPeer:
    return _RtcPeer(
        instance_id="peer-1",
        signaling=_noop_signaling,
        inbound=_noop_inbound,
        media_inbound=media_inbound,
    )


async def _noop_signaling(event_type, payload):
    return None


async def _noop_inbound(envelope):
    return None


# ─── send_media ────────────────────────────────────────────────────────────


async def test_send_media_false_when_channel_not_ready():
    peer = _peer()
    assert peer.is_media_ready is False
    assert await peer.send_media(b"hdr", b"payload") is False


async def test_send_media_sends_frame_when_ready():
    peer = _peer()
    ch = _ScriptedChannel()
    peer._media_channel = ch
    peer._media_open.set()
    ok = await peer.send_media(b'{"msg_id":"x"}', b"\x00\x01\x02bytes")
    assert ok is True
    assert len(ch.sent) == 1
    frame = mf.decode(ch.sent[0])
    assert frame.header == b'{"msg_id":"x"}'
    assert frame.payload == b"\x00\x01\x02bytes"


async def test_send_media_false_over_hwm():
    peer = _peer()
    ch = _ScriptedChannel()
    ch.buffered_amount = 1 << 21  # well over the 1 MiB HWM
    peer._media_channel = ch
    peer._media_open.set()
    assert await peer.send_media(b"h", b"p") is False
    assert ch.sent == []


async def test_send_media_false_on_send_error():
    peer = _peer()
    ch = _ScriptedChannel()
    ch.raise_on_send = rtc.RTCError("boom")
    peer._media_channel = ch
    peer._media_open.set()
    assert await peer.send_media(b"h", b"p") is False


# ─── _drain_media_channel ────────────────────────────────────────────────


async def test_drain_media_channel_routes_frame_to_callback():
    received: list = []

    async def cb(instance_id, header, payload):
        received.append((instance_id, header, payload))

    peer = _peer(media_inbound=cb)
    frame = mf.encode(b"header-bytes", b"payload-bytes")
    ch = _ScriptedChannel([frame])
    await peer._drain_media_channel(ch)
    assert received == [("peer-1", b"header-bytes", b"payload-bytes")]


async def test_drain_media_channel_reassembles_split_frame():
    received: list = []

    async def cb(instance_id, header, payload):
        received.append((header, payload))

    peer = _peer(media_inbound=cb)
    frame = mf.encode(b"hdr", b"some-payload-bytes")
    # Two SCTP messages that together make one frame.
    ch = _ScriptedChannel([frame[:6], frame[6:]])
    await peer._drain_media_channel(ch)
    assert received == [(b"hdr", b"some-payload-bytes")]


async def test_drain_media_channel_skips_unknown_frame_type():
    received: list = []

    async def cb(instance_id, header, payload):
        received.append((header, payload))

    peer = _peer(media_inbound=cb)
    frame = mf.encode(b"hdr", b"p", frame_type=99)
    ch = _ScriptedChannel([frame])
    await peer._drain_media_channel(ch)
    assert received == []  # unknown type skipped, no crash


async def test_drain_media_channel_resets_buffer_on_malformed():
    received: list = []

    async def cb(instance_id, header, payload):
        received.append((header, payload))

    peer = _peer(media_inbound=cb)
    import struct

    # frame_type=1 with a declared header_len over the ceiling.
    bad = struct.pack(">BI", 1, mf.MAX_HEADER_BYTES + 1)
    good = mf.encode(b"hdr", b"payload")
    ch = _ScriptedChannel([bad, good])
    await peer._drain_media_channel(ch)
    # Buffer reset after the bad message; the good one still lands.
    assert received == [(b"hdr", b"payload")]


# ─── Channel lifecycle ───────────────────────────────────────────────────


async def test_start_offer_creates_both_channels():
    """start_offer creates the control, media, and app channels on one PC."""
    peer = _peer()
    await peer.start_offer()
    labels = {ch.label for ch in peer._pc._channels}
    assert labels == {"fed-v1", mf.CHANNEL_LABEL, af.CHANNEL_LABEL}
    assert peer._channel is not None
    assert peer._media_channel is not None
    peer.close()


async def test_answerer_drain_incoming_latches_both_channels():
    peer = _peer()
    peer._pc = rtc.PeerConnection()
    fed = _ScriptedChannel(label="fed-v1")
    media = _ScriptedChannel(label=mf.CHANNEL_LABEL)
    junk = _ScriptedChannel(label="some-other")
    peer._pc._incoming_queue.put_nowait(fed)
    peer._pc._incoming_queue.put_nowait(media)
    peer._pc._incoming_queue.put_nowait(junk)
    peer._pc._incoming_queue.put_nowait(None)  # end the iterator
    await peer._drain_incoming_channel()
    assert peer._channel is fed
    assert peer._media_channel is media


# ─── Facade send_media ──────────────────────────────────────────────────


async def test_facade_send_media_false_without_peer():
    t = FederationTransport(
        own_instance_id="self",
        https_inbox=object(),  # never touched on this path
        signaling_send=_noop_signaling,
    )
    inst = _fake_instance("peer-1")
    assert (
        await t.send_media(
            instance=inst, header_dict={"msg_id": "x"}, payload_bytes=b"p"
        )
        is False
    )
    assert t.is_media_ready("peer-1") is False


async def test_facade_send_media_delegates_to_ready_peer():
    t = FederationTransport(
        own_instance_id="self",
        https_inbox=object(),
        signaling_send=_noop_signaling,
    )
    inst = _fake_instance("peer-1")
    peer = _peer()
    ch = _ScriptedChannel()
    peer._media_channel = ch
    peer._media_open.set()
    t._peers["peer-1"] = peer
    assert t.is_media_ready("peer-1") is True
    ok = await t.send_media(
        instance=inst,
        header_dict={"msg_id": "x", "event_type": "dm_media_blob"},
        payload_bytes=b"raw",
    )
    assert ok is True
    frame = mf.decode(ch.sent[0])
    assert orjson.loads(frame.header)["msg_id"] == "x"
    assert frame.payload == b"raw"


async def test_drain_media_channel_coerces_string_message():
    """A str on the binary channel (protocol violation) is coerced to
    bytes and rejected by framing without crashing the drain loop."""
    received: list = []

    async def cb(instance_id, header, payload):
        received.append((header, payload))

    peer = _peer(media_inbound=cb)
    # A short non-frame string → framing sees an incomplete frame
    # (leftover), so nothing dispatches and the loop survives.
    ch = _ScriptedChannel(["not a frame"])
    await peer._drain_media_channel(ch)
    assert received == []


async def test_facade_send_media_false_when_peer_raises():
    """A peer whose send_media raises an unexpected error → facade
    catches and reports False (caller falls back to JSON)."""

    class _RaisingPeer:
        def __init__(self):
            self.instance_id = "peer-1"

        @property
        def is_media_ready(self):
            return True

        async def send_media(self, header, payload):
            raise RuntimeError("unexpected")

    t = FederationTransport(
        own_instance_id="self",
        https_inbox=object(),
        signaling_send=_noop_signaling,
    )
    t._peers["peer-1"] = _RaisingPeer()
    ok = await t.send_media(
        instance=_fake_instance("peer-1"),
        header_dict={"msg_id": "x"},
        payload_bytes=b"p",
    )
    assert ok is False
