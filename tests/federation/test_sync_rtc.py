"""Tests for SyncRtcSession (§4.2.3, §25.6.2)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import aiolibdatachannel as rtc
import pytest

from socialhome.federation.sync_rtc import (
    CHANNEL_LABEL,
    ICE_TIMEOUT_SECONDS,
    MAX_SIGNALING_SESSIONS,
    SyncRtcSession,
)

# ─── Constants ────────────────────────────────────────────────────────────


def test_channel_label_is_distinct_from_gfs():
    """sync-v1 must differ from the GFS gfs-v1 channel label."""
    assert CHANNEL_LABEL == "sync-v1"
    assert CHANNEL_LABEL != "gfs-v1"


def test_ice_timeout_default_matches_spec():
    """§4.2.3 says 15s timeout on ICE negotiation."""
    assert ICE_TIMEOUT_SECONDS == 15.0


def test_max_signaling_sessions_matches_audit_s8():
    """S-8 cap: 200 sessions per node."""
    assert MAX_SIGNALING_SESSIONS == 200


# ─── Construction guards ──────────────────────────────────────────────────


def test_invalid_sync_mode_rejected():
    """sync_mode is a formal field per S-16."""
    with pytest.raises(ValueError):
        SyncRtcSession(
            sync_id="sid",
            space_id="sp1",
            requester_instance_id="r",
            provider_instance_id="p",
            sync_mode="bogus",
        )


def test_invalid_role_rejected():
    """role must be provider or requester."""
    with pytest.raises(ValueError):
        SyncRtcSession(
            sync_id="sid",
            space_id="sp1",
            requester_instance_id="r",
            provider_instance_id="p",
            role="snoop",
        )


async def test_default_role_is_provider():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
    )
    assert s.role == "provider"
    assert s.sync_mode == "initial"


async def test_requester_id_is_a_formal_field_s14():
    """S-14: requester_instance_id is persisted on the session."""
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="alice-instance",
        provider_instance_id="bob-instance",
    )
    assert s.requester_instance_id == "alice-instance"
    assert s.provider_instance_id == "bob-instance"


# ─── S-13: create_answer vs set_answer ────────────────────────────────────


async def test_provider_create_offer_returns_sdp():
    s = SyncRtcSession(
        sync_id="sid1",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        role="provider",
    )
    sdp = await s.create_offer()
    assert sdp.startswith("v=0")
    assert isinstance(sdp, str)


async def test_requester_cannot_create_offer_s13():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        role="requester",
    )
    with pytest.raises(RuntimeError):
        await s.create_offer()


async def test_provider_cannot_create_answer_s13():
    """S-13: create_answer is a *requester* operation only."""
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        role="provider",
    )
    with pytest.raises(RuntimeError):
        await s.create_answer("v=0\r\n")


async def test_requester_create_answer_returns_distinct_sdp_s13():
    """S-13 fix: create_answer (requester) is NOT set_answer."""
    s = SyncRtcSession(
        sync_id="sid2",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        role="requester",
    )
    answer = await s.create_answer("v=0\r\no=- 0 0 IN IP4 1.2.3.4\r\n")
    assert answer.startswith("v=0")
    assert isinstance(answer, str)


async def test_requester_cannot_set_answer():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        role="requester",
    )
    with pytest.raises(RuntimeError):
        await s.set_answer("v=0\r\n")


async def test_provider_set_answer_records_remote_sdp():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        role="provider",
    )
    await s.create_offer()
    await s.set_answer("v=0\r\nthe-answer\r\n")
    assert s._remote_sdp == "v=0\r\nthe-answer\r\n"


async def test_set_answer_rejects_empty_sdp():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        role="provider",
    )
    with pytest.raises(ValueError):
        await s.set_answer("")


async def test_create_answer_rejects_empty_sdp():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        role="requester",
    )
    with pytest.raises(ValueError):
        await s.create_answer("")


# ─── ICE candidate handling ───────────────────────────────────────────────


async def test_add_ice_candidate_appends():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
    )
    await s.add_ice_candidate("candidate:1 1 UDP 2122252543 1.2.3.4 1234 typ host")
    assert len(s._ice_candidates) == 1


async def test_add_ice_candidate_rejects_empty():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
    )
    with pytest.raises(ValueError):
        await s.add_ice_candidate("")


_CAND = "candidate:1 1 UDP 2122252543 1.2.3.4 1234 typ host"


async def test_late_ice_candidate_for_a_closed_session_is_dropped():
    """A candidate trailing a session that already closed never reaches the
    native layer (which would log an ERROR and raise — the SPACE_SYNC_ICE
    handler traceback the federation demo tripped on)."""
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
    )
    s._pc = MagicMock(closed=False, state=rtc.RTCState.CLOSED)
    s._pc.add_remote_candidate = AsyncMock()
    await s.add_ice_candidate(_CAND)
    s._pc.add_remote_candidate.assert_not_awaited()
    s._pc = MagicMock(closed=False, state=rtc.RTCState.CONNECTED)
    s._pc.ice_state = rtc.ICEState.CLOSED
    s._pc.add_remote_candidate = AsyncMock()
    await s.add_ice_candidate(_CAND)
    s._pc.add_remote_candidate.assert_not_awaited()


async def test_ice_candidate_the_native_layer_rejects_does_not_raise():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
    )
    s._pc = MagicMock(
        closed=False, state=rtc.RTCState.CONNECTED, ice_state=rtc.ICEState.CONNECTED
    )
    s._pc.add_remote_candidate = AsyncMock(side_effect=rtc.RTCError("no transport"))
    await s.add_ice_candidate(_CAND)
    s._pc.add_remote_candidate.assert_awaited_once()


class _RacyPc(rtc.PeerConnection):
    """aiolibdatachannel's PeerConnection as far as this race goes: a call
    that applies remote state runs in an executor thread (here: waits for
    :attr:`gate`) and only then reaches the native handle, while
    ``close()`` schedules the teardown that deletes that handle on its
    own. A call that reaches a deleted handle is what libdatachannel logs
    as ``PeerConnection ID does not exist`` (ERROR) — recorded in
    :attr:`after_destroy`."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.reached = asyncio.Event()
        self.destroyed = False
        self.after_destroy: list[str] = []

    async def _native_call(self, name: str) -> None:
        # The executor job outlives a cancelled awaiter, like
        # ``run_in_executor``'s thread does.
        self.reached.set()
        await asyncio.shield(asyncio.ensure_future(self._job(name)))

    async def _job(self, name: str) -> None:
        await self.gate.wait()
        if self.destroyed:
            self.after_destroy.append(name)

    async def add_remote_candidate(self, candidate: str, mid: str = "") -> None:
        await self._native_call("add_remote_candidate")

    async def set_remote_description(self, sdp: str, type_: str) -> None:
        await super().set_remote_description(sdp, type_)
        await self._native_call("set_remote_description")

    def close(self) -> None:
        super().close()
        asyncio.get_running_loop().call_soon(setattr, self, "destroyed", True)


def _session(role: str) -> tuple[SyncRtcSession, _RacyPc]:
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        role=role,
    )
    s._pc = pc = _RacyPc()
    return s, pc


async def _close_while_in_flight(s: SyncRtcSession, pc: _RacyPc, call) -> None:
    """``close()`` while ``call`` is in the executor; then let it land."""
    task = asyncio.create_task(call)
    await pc.reached.wait()
    s.close()
    for _ in range(3):  # the teardown runs before the executor job lands
        await asyncio.sleep(0)
    pc.gate.set()
    try:
        await task
    except ConnectionError:
        pass
    for _ in range(10):
        await asyncio.sleep(0)


async def test_close_never_deletes_the_pc_under_an_in_flight_ice_candidate():
    """A remote candidate that arrives as the stream ends (the provider's
    late srflx candidate, trickled via SPACE_SYNC_ICE) passes the closed
    check and goes to the executor — and ``close()`` deleted the native
    peer connection before the executor thread reached it: libdatachannel's
    ``PeerConnection ID does not exist`` ERROR in the federation demo.
    ``close()`` now waits for the in-flight call, then closes."""
    s, pc = _session("requester")
    await _close_while_in_flight(s, pc, s.add_ice_candidate(_CAND))
    assert pc.after_destroy == []
    assert pc.closed and pc.destroyed
    assert s.is_closed


async def test_close_never_deletes_the_pc_under_an_in_flight_answer():
    """The same window for the provider applying the requester's answer."""
    s, pc = _session("provider")
    await s.create_offer()
    await _close_while_in_flight(s, pc, s.set_answer("v=0\r\nanswer\r\n"))
    assert pc.after_destroy == []
    assert pc.closed and pc.destroyed


async def test_create_answer_after_a_close_mid_way_is_a_connection_error():
    """``close()`` landing while the requester applies the offer: no
    answer is built on the torn-down session (used to be an
    ``AttributeError`` on ``None``), and nothing reaches a deleted pc."""
    s, pc = _session("requester")
    task = asyncio.create_task(s.create_answer("v=0\r\noffer\r\n"))
    await pc.reached.wait()
    s.close()
    await asyncio.sleep(0)
    pc.gate.set()
    with pytest.raises(ConnectionError):
        await task
    await asyncio.sleep(0)
    assert pc.after_destroy == []
    assert pc.closed and pc.destroyed


async def test_a_cancelled_caller_still_holds_off_the_close():
    """The SPACE_SYNC_ICE handler cancelled mid-call (shutdown): the
    executor job runs on regardless, so the close still waits for it."""
    s, pc = _session("requester")
    task = asyncio.create_task(s.add_ice_candidate(_CAND))
    await pc.reached.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    s.close()
    for _ in range(3):
        await asyncio.sleep(0)
    assert not pc.closed
    pc.gate.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert pc.after_destroy == []
    assert pc.closed and pc.destroyed


async def test_watchers_stand_down_while_the_close_waits():
    """Between ``close()`` and the deferred ``pc.close()`` the pc's
    watcher tasks still run: a data channel arriving then is not adopted
    (it used to be, then ``spawn_task`` on ``None`` raised), and the
    session does not turn ready again."""
    s, pc = _session("requester")
    watcher = pc.spawn_task(s._watch_incoming())
    in_flight = asyncio.create_task(s.add_ice_candidate(_CAND))
    await pc.reached.wait()
    s.close()
    channel = await pc.create_data_channel(CHANNEL_LABEL)
    pc._incoming_queue.put_nowait(channel)
    for _ in range(5):
        await asyncio.sleep(0)
    assert watcher.done() and watcher.exception() is None
    assert s._channel is None
    await s._watch_channel(channel)
    assert not s.is_ready
    pc.gate.set()
    await in_flight
    for _ in range(10):
        await asyncio.sleep(0)
    assert pc.closed and pc.after_destroy == []


async def test_no_native_call_starts_after_close():
    s, pc = _session("requester")
    s.close()
    await asyncio.sleep(0)
    with pytest.raises(ConnectionError):
        await s.create_answer("v=0\r\noffer\r\n")
    await s.add_ice_candidate(_CAND)  # dropped quietly, as before
    assert not pc.reached.is_set()


# ─── Lifecycle ────────────────────────────────────────────────────────────


async def test_wait_ready_returns_false_in_stub_mode():
    """Without aiolibdatachannel, the channel never opens — caller must fall back."""
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
    )
    ready = await s.wait_ready(timeout=0.05)
    assert ready is False


async def test_send_chunk_raises_when_channel_closed():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
    )
    s.close()
    with pytest.raises(ConnectionError):
        await s.send_chunk(b"data")


async def test_close_marks_session_closed():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
    )
    s.close()
    assert s.is_closed
    assert not s.is_ready


async def test_local_ice_drain_invokes_callback_per_candidate():
    """Issue #412 item 1: ``SyncRtcSession`` now forwards each
    locally-gathered ICE candidate via the injected ``on_local_ice``
    callback. Same-LAN handshakes used to work because host
    candidates are inline in the SDP; without this drain, cross-NAT
    handshakes hit the 15 s connectivity timeout because the
    reflexive / relayed candidates were never sent."""
    emitted: list[tuple[str, str, str]] = []

    async def _capture(sync_id: str, cand: str, mid: str) -> None:
        emitted.append((sync_id, cand, mid))

    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        role="provider",
        on_local_ice=_capture,
    )
    # Push two ICE candidates into the stub's queue *before*
    # ``create_offer`` to ensure the drain task picks them up.
    from aiolibdatachannel import IceCandidate

    s._pc._ice_queue.put_nowait(IceCandidate(candidate="cand-A", mid="0"))
    s._pc._ice_queue.put_nowait(IceCandidate(candidate="cand-B", mid="1"))
    await s.create_offer()
    # Yield to let the spawn task drain both candidates.
    import asyncio

    for _ in range(50):
        await asyncio.sleep(0)
        if len(emitted) == 2:
            break
    assert emitted == [("sid", "cand-A", "0"), ("sid", "cand-B", "1")]
    # Tear down so the spawn task doesn't linger past test exit.
    s.close()


async def test_local_ice_drain_skipped_without_callback():
    """Without the callback there's no drain task — backward-compat
    for callers that don't wire the outbound path (older test code,
    or sites where the routing layer doesn't yet need it)."""
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
        role="provider",
    )
    # No on_local_ice → ``_drain_local_ice`` returns early. Nothing
    # to assert beyond "doesn't raise" — the legacy code path stays
    # functional.
    await s.create_offer()
    s.close()


async def test_recv_chunk_reports_a_closed_channel_as_connection_error():
    """The library raises its own ``ConnectionClosedError`` (a
    ``RuntimeError``) when the channel closes; the documented contract the
    requester's drain relies on is ``ConnectionError``."""
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
    )
    s._channel = MagicMock()
    s._channel.recv = AsyncMock(side_effect=rtc.ConnectionClosedError("closed"))
    with pytest.raises(ConnectionError):
        await s.recv_chunk()


async def test_recv_chunk_returns_the_next_frame():
    s = SyncRtcSession(
        sync_id="sid",
        space_id="sp1",
        requester_instance_id="r",
        provider_instance_id="p",
    )
    s._channel = MagicMock()
    s._channel.recv = AsyncMock(return_value=b"frame")
    assert await s.recv_chunk() == b"frame"
