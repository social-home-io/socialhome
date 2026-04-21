"""Coverage fill for :class:`SyncRtcSession` — drain loops + config helper."""

from __future__ import annotations

import asyncio

from social_home.federation.sync_rtc import (
    CHANNEL_LABEL,
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

        async def send(self, data):
            self.sent.append(data)

    ch = _FakeCh()
    s._channel = ch  # type: ignore[attr-defined]
    await s.send_chunk(b"hello")
    assert ch.sent == [b"hello"]


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
            raise RuntimeError("handshake failed")

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

    s._pc = _FakePc()  # type: ignore[attr-defined]
    await s._watch_incoming()
    assert s._channel is good
