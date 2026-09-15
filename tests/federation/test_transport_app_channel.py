"""Tests for the binary app DataChannel (``fed-app-v1``) on the
federation transport — ``_RtcPeer`` third-channel lifecycle, framing
round-trip over the channel, and the facade ``send_app``.

Mirrors ``test_transport_media_channel.py`` touchpoint-for-touchpoint,
swapping every ``_media_*`` reference for ``_app_*`` and
``media_framing`` / ``mf`` for ``app_framing`` / ``af``.

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

    def __init__(self, messages=None, *, label=af.CHANNEL_LABEL):
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


def _peer(app_inbound=None) -> _RtcPeer:
    return _RtcPeer(
        instance_id="peer-1",
        signaling=_noop_signaling,
        inbound=_noop_inbound,
        app_inbound=app_inbound,
    )


async def _noop_signaling(event_type, payload):
    return None


async def _noop_inbound(envelope):
    return None


# ─── send_app ──────────────────────────────────────────────────────────────


async def test_send_app_false_when_channel_not_ready():
    peer = _peer()
    assert peer.is_app_ready is False
    assert await peer.send_app(b"hdr", b"payload") is False


async def test_send_app_sends_frame_when_ready():
    peer = _peer()
    ch = _ScriptedChannel()
    peer._app_channel = ch
    peer._app_open.set()
    ok = await peer.send_app(b'{"msg_id":"x"}', b"\x00\x01\x02bytes")
    assert ok is True
    assert len(ch.sent) == 1
    frame = af.decode(ch.sent[0])
    assert frame.header == b'{"msg_id":"x"}'
    assert frame.payload == b"\x00\x01\x02bytes"


async def test_send_app_false_over_hwm():
    peer = _peer()
    ch = _ScriptedChannel()
    ch.buffered_amount = 1 << 21  # well over the 1 MiB HWM
    peer._app_channel = ch
    peer._app_open.set()
    assert await peer.send_app(b"h", b"p") is False
    assert ch.sent == []


async def test_send_app_false_on_send_error():
    peer = _peer()
    ch = _ScriptedChannel()
    ch.raise_on_send = rtc.RTCError("boom")
    peer._app_channel = ch
    peer._app_open.set()
    assert await peer.send_app(b"h", b"p") is False


# ─── _drain_app_channel ──────────────────────────────────────────────────


async def test_drain_app_channel_routes_frame_to_callback():
    received: list = []

    async def cb(instance_id, header, payload):
        received.append((instance_id, header, payload))

    peer = _peer(app_inbound=cb)
    frame = af.encode(b"header-bytes", b"payload-bytes")
    ch = _ScriptedChannel([frame])
    await peer._drain_app_channel(ch)
    assert received == [("peer-1", b"header-bytes", b"payload-bytes")]


async def test_drain_app_channel_reassembles_split_frame():
    received: list = []

    async def cb(instance_id, header, payload):
        received.append((header, payload))

    peer = _peer(app_inbound=cb)
    frame = af.encode(b"hdr", b"some-payload-bytes")
    # Two SCTP messages that together make one frame.
    ch = _ScriptedChannel([frame[:6], frame[6:]])
    await peer._drain_app_channel(ch)
    assert received == [(b"hdr", b"some-payload-bytes")]


async def test_drain_app_channel_skips_unknown_frame_type():
    received: list = []

    async def cb(instance_id, header, payload):
        received.append((header, payload))

    peer = _peer(app_inbound=cb)
    frame = af.encode(b"hdr", b"p", frame_type=99)
    ch = _ScriptedChannel([frame])
    await peer._drain_app_channel(ch)
    assert received == []  # unknown type skipped, no crash


async def test_drain_app_channel_resets_buffer_on_malformed():
    received: list = []

    async def cb(instance_id, header, payload):
        received.append((header, payload))

    peer = _peer(app_inbound=cb)
    import struct

    # frame_type=1 with a declared header_len over the ceiling.
    bad = struct.pack(">BI", 1, af.MAX_HEADER_BYTES + 1)
    good = af.encode(b"hdr", b"payload")
    ch = _ScriptedChannel([bad, good])
    await peer._drain_app_channel(ch)
    # Buffer reset after the bad message; the good one still lands.
    assert received == [(b"hdr", b"payload")]


# ─── Channel lifecycle ───────────────────────────────────────────────────


async def test_start_offer_creates_all_three_channels():
    peer = _peer()
    await peer.start_offer()
    labels = {ch.label for ch in peer._pc._channels}
    assert labels == {"fed-v1", mf.CHANNEL_LABEL, af.CHANNEL_LABEL}
    assert peer._channel is not None
    assert peer._media_channel is not None
    assert peer._app_channel is not None
    peer.close()


async def test_answerer_drain_incoming_latches_all_three_channels():
    peer = _peer()
    peer._pc = rtc.PeerConnection()
    fed = _ScriptedChannel(label="fed-v1")
    media = _ScriptedChannel(label=mf.CHANNEL_LABEL)
    app = _ScriptedChannel(label=af.CHANNEL_LABEL)
    junk = _ScriptedChannel(label="some-other")
    peer._pc._incoming_queue.put_nowait(fed)
    peer._pc._incoming_queue.put_nowait(media)
    peer._pc._incoming_queue.put_nowait(app)
    peer._pc._incoming_queue.put_nowait(junk)
    peer._pc._incoming_queue.put_nowait(None)  # end the iterator
    await peer._drain_incoming_channel()
    assert peer._channel is fed
    assert peer._media_channel is media
    assert peer._app_channel is app


async def test_is_app_ready_reflects_app_open_event():
    peer = _peer()
    assert peer.is_app_ready is False
    ch = _ScriptedChannel()
    peer._app_channel = ch
    peer._app_open.set()
    assert peer.is_app_ready is True
    peer._closed = True
    assert peer.is_app_ready is False


# ─── Facade send_app ────────────────────────────────────────────────────


async def test_facade_send_app_false_without_peer():
    t = FederationTransport(
        own_instance_id="self",
        https_inbox=object(),  # never touched on this path
        signaling_send=_noop_signaling,
    )
    inst = _fake_instance("peer-1")
    assert (
        await t.send_app(instance=inst, header_dict={"msg_id": "x"}, payload_bytes=b"p")
        is False
    )
    assert t.is_app_ready("peer-1") is False


async def test_facade_send_app_delegates_to_ready_peer():
    t = FederationTransport(
        own_instance_id="self",
        https_inbox=object(),
        signaling_send=_noop_signaling,
    )
    inst = _fake_instance("peer-1")
    peer = _peer()
    ch = _ScriptedChannel()
    peer._app_channel = ch
    peer._app_open.set()
    t._peers["peer-1"] = peer
    assert t.is_app_ready("peer-1") is True
    ok = await t.send_app(
        instance=inst,
        header_dict={"msg_id": "x", "event_type": "app_message"},
        payload_bytes=b"raw",
    )
    assert ok is True
    frame = af.decode(ch.sent[0])
    assert orjson.loads(frame.header)["msg_id"] == "x"
    assert frame.payload == b"raw"


async def test_drain_app_channel_coerces_string_message():
    """A str on the binary channel (protocol violation) is coerced to
    bytes and rejected by framing without crashing the drain loop."""
    received: list = []

    async def cb(instance_id, header, payload):
        received.append((header, payload))

    peer = _peer(app_inbound=cb)
    # A short non-frame string → framing sees an incomplete frame
    # (leftover), so nothing dispatches and the loop survives.
    ch = _ScriptedChannel(["not a frame"])
    await peer._drain_app_channel(ch)
    assert received == []


async def test_facade_send_app_false_when_peer_raises():
    """A peer whose send_app raises an unexpected error → facade
    catches and reports False (caller falls back to JSON)."""

    class _RaisingPeer:
        def __init__(self):
            self.instance_id = "peer-1"

        @property
        def is_app_ready(self):
            return True

        async def send_app(self, header, payload):
            raise RuntimeError("unexpected")

    t = FederationTransport(
        own_instance_id="self",
        https_inbox=object(),
        signaling_send=_noop_signaling,
    )
    t._peers["peer-1"] = _RaisingPeer()
    ok = await t.send_app(
        instance=_fake_instance("peer-1"),
        header_dict={"msg_id": "x"},
        payload_bytes=b"p",
    )
    assert ok is False


async def test_drain_app_channel_survives_bad_callback_and_delivers_next():
    """A callback that raises on one frame (e.g. ValueError from bad sha256
    or UnsupportedAppAeadSuite) must NOT kill the drain task — a subsequent
    good frame on the same channel must still be delivered (I1 fix).
    """
    received: list = []
    call_count = 0

    async def cb(instance_id, header, payload):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Simulate a validation error on the first frame.
            raise ValueError("bad payload_sha256 on forged frame")
        received.append((instance_id, header, payload))

    peer = _peer(app_inbound=cb)
    good_frame = af.encode(b"header-ok", b"payload-ok")
    # Two valid binary frames; the callback raises on the first, succeeds on
    # the second.  Both frames are parsed correctly by iter_complete_frames —
    # the exception lives inside the callback, not in framing.
    ch = _ScriptedChannel([good_frame, good_frame])
    await peer._drain_app_channel(ch)
    # The second frame must have reached the callback despite the first raising.
    assert len(received) == 1
    assert received[0] == ("peer-1", b"header-ok", b"payload-ok")
