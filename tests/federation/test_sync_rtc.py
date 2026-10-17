"""Tests for SyncRtcSession (§4.2.3, §25.6.2)."""

from __future__ import annotations

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
