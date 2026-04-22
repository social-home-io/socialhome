"""Backpressure tests for the two DataChannel-send call sites.

* :class:`~socialhome.federation.transport._RtcPeer.send` — drops the
  frame under backpressure, returns ``False`` so the facade falls back
  to webhook.
* :class:`~socialhome.federation.sync_rtc.SyncRtcSession.send_chunk` —
  raises :class:`ConnectionError` so :class:`SyncSessionManager` falls
  back to relay chunks.

These complement the happy-path tests in ``test_transport_coverage.py``
and ``test_sync_rtc_coverage.py`` — here we exclusively exercise the
`buffered_amount >= HWM` branch.
"""

from __future__ import annotations

import pytest

from socialhome.federation.sync_rtc import SEND_HWM_BYTES, SyncRtcSession
from socialhome.federation.transport import SEND_HWM_BYTES as FED_HWM, _RtcPeer


# ── fed-v1 transport ────────────────────────────────────────────────


async def _noop_signal(event_type, payload):
    return None


async def _noop_inbound(data):
    return None


class _Channel:
    """Fake DataChannel with a settable ``buffered_amount``."""

    def __init__(self, buffered: int) -> None:
        self.buffered_amount = buffered
        self.sent: list = []

    async def send(self, data):
        self.sent.append(data)


@pytest.mark.parametrize(
    "buffered, expect_send",
    [
        (0, True),  # empty buffer → send
        (FED_HWM - 1, True),  # one byte below HWM → still sends
        (FED_HWM, False),  # exactly at HWM → drop
        (FED_HWM * 2, False),  # way over HWM → drop
    ],
)
async def test_fed_peer_send_respects_hwm(buffered: int, expect_send: bool):
    peer = _RtcPeer(
        instance_id="p",
        ice_servers=None,
        signaling=_noop_signal,
        inbound=_noop_inbound,
    )
    ch = _Channel(buffered=buffered)
    peer._channel = ch  # type: ignore[attr-defined]
    peer._open.set()

    ok = await peer.send({"x": 1})
    assert ok is expect_send
    # Sanity: send() was only invoked on the channel when we were below HWM.
    assert (len(ch.sent) > 0) is expect_send


async def test_fed_peer_send_hwm_is_configurable():
    """The HWM is a constructor parameter — lets ops tune it per deployment."""
    peer = _RtcPeer(
        instance_id="p",
        ice_servers=None,
        signaling=_noop_signal,
        inbound=_noop_inbound,
        send_hwm=128,
    )
    ch = _Channel(buffered=256)
    peer._channel = ch  # type: ignore[attr-defined]
    peer._open.set()

    assert await peer.send({"x": 1}) is False


# ── sync-v1 transport ───────────────────────────────────────────────


async def test_sync_send_chunk_under_hwm_succeeds():
    s = SyncRtcSession(
        sync_id="s1",
        space_id="sp",
        requester_instance_id="r",
        provider_instance_id="p",
        role="provider",
    )
    await s.create_offer()

    class _Ch:
        def __init__(self) -> None:
            self.sent: list = []
            self.buffered_amount = SEND_HWM_BYTES - 1

        async def send(self, data):
            self.sent.append(data)

    ch = _Ch()
    s._channel = ch  # type: ignore[attr-defined]
    await s.send_chunk(b"chunk")
    assert ch.sent == [b"chunk"]


async def test_sync_send_chunk_raises_at_hwm():
    s = SyncRtcSession(
        sync_id="s2",
        space_id="sp",
        requester_instance_id="r",
        provider_instance_id="p",
        role="provider",
    )
    await s.create_offer()

    class _Ch:
        def __init__(self) -> None:
            self.sent: list = []
            self.buffered_amount = SEND_HWM_BYTES

        async def send(self, data):
            self.sent.append(data)

    ch = _Ch()
    s._channel = ch  # type: ignore[attr-defined]
    with pytest.raises(ConnectionError, match="backpressured"):
        await s.send_chunk(b"chunk")
    assert ch.sent == []


async def test_sync_send_chunk_raises_when_closed():
    s = SyncRtcSession(
        sync_id="s3",
        space_id="sp",
        requester_instance_id="r",
        provider_instance_id="p",
        role="provider",
    )
    # Not opened — _channel is None.
    with pytest.raises(ConnectionError, match="not open"):
        await s.send_chunk(b"chunk")
