"""Tests for GfsRtcSession, the public-highlight signalling store."""

from __future__ import annotations

import pytest

from socialhome.global_server import rtc_transport
from socialhome.global_server.rtc_transport import GfsRtcSession


@pytest.fixture
def rtc():
    """A fresh GfsRtcSession instance."""
    return GfsRtcSession()


async def test_offer_returns_session_id(rtc):
    """offer() returns a non-empty string session_id."""
    session_id = await rtc.offer("inst-a", "v=0\r\no=...\r\n", client="c")
    assert isinstance(session_id, str)
    assert len(session_id) > 0


async def test_offer_returns_unique_ids(rtc):
    """Multiple offer() calls return distinct session_ids."""
    id1 = await rtc.offer("inst-a", "sdp1", client="c")
    id2 = await rtc.offer("inst-a", "sdp2", client="c")
    assert id1 != id2


async def test_offer_stores_session(rtc):
    """offer() stores session state retrievable via get_session()."""
    session_id = await rtc.offer("inst-x", "sdp-body", client="c")
    session = rtc.get_session(session_id)
    assert session is not None
    assert session.session_id == session_id
    assert session.initiator_id == "inst-x"
    assert session.offer_sdp == "sdp-body"
    assert session.answer_sdp is None


async def test_answer_stores_answer_sdp(rtc):
    """answer() stores the answer SDP for the given session."""
    session_id = await rtc.offer("inst-a", "offer-sdp", client="c")
    await rtc.answer(session_id, "answer-sdp")
    session = rtc.get_session(session_id)
    assert session is not None
    assert session.answer_sdp == "answer-sdp"


async def test_answer_unknown_session_raises_key_error(rtc):
    """answer() with an unknown session_id raises KeyError."""
    with pytest.raises(KeyError):
        await rtc.answer("nonexistent-session-id", "sdp")


async def test_ice_candidate_accumulates_candidates(rtc):
    """ice_candidate() appends candidates to the session."""
    session_id = await rtc.offer("inst-b", "sdp", client="c")
    candidate1 = {"candidate": "candidate:0 1 UDP ...", "sdpMid": "0"}
    candidate2 = {"candidate": "candidate:1 1 TCP ...", "sdpMid": "0"}
    await rtc.ice_candidate(session_id, candidate1)
    await rtc.ice_candidate(session_id, candidate2)
    session = rtc.get_session(session_id)
    assert session is not None
    assert len(session.ice_candidates) == 2
    assert session.ice_candidates[0] == candidate1
    assert session.ice_candidates[1] == candidate2


async def test_ice_candidate_unknown_session_raises_key_error(rtc):
    """ice_candidate() with an unknown session_id raises KeyError."""
    with pytest.raises(KeyError):
        await rtc.ice_candidate("no-such-session", {"candidate": "x"})


async def test_get_session_returns_none_for_unknown(rtc):
    """get_session() returns None for an unknown session_id."""
    result = rtc.get_session("does-not-exist")
    assert result is None


async def test_multiple_sessions_are_independent(rtc):
    """Two concurrent sessions do not interfere with each other."""
    sid1 = await rtc.offer("inst-1", "sdp-1", client="c")
    sid2 = await rtc.offer("inst-2", "sdp-2", client="c")
    await rtc.answer(sid1, "answer-1")
    s1 = rtc.get_session(sid1)
    s2 = rtc.get_session(sid2)
    assert s1.answer_sdp == "answer-1"
    assert s2.answer_sdp is None


async def test_offer_with_empty_sdp_is_accepted(rtc):
    """offer() accepts an empty SDP string without raising."""
    session_id = await rtc.offer("inst-empty", "", client="c")
    session = rtc.get_session(session_id)
    assert session is not None
    assert session.offer_sdp == ""


async def test_a_session_id_names_the_node_holding_it():
    rtc = GfsRtcSession(route_tag="tag-a")
    session_id = await rtc.offer("inst-a", "sdp", client="c")
    assert session_id is not None and session_id.startswith("tag-a.")


async def test_offer_sweeps_expired_sessions(rtc):
    stale = await rtc.offer("inst-a", "sdp", client="c")
    rtc.get_session(stale).created_at = 0.0
    await rtc.offer("inst-a", "sdp", client="c")
    assert rtc.get_session(stale) is None
    assert rtc.session_count() == 1


async def test_offer_sheds_past_the_live_cap(rtc, monkeypatch):
    monkeypatch.setattr(rtc_transport, "MAX_LIVE_SESSIONS", 2)
    assert await rtc.offer("inst-a", "sdp", client="c") is not None
    kept = await rtc.offer("inst-a", "sdp", client="c")
    assert await rtc.offer("inst-a", "sdp", client="c") is None
    rtc.drop(kept)
    assert await rtc.offer("inst-a", "sdp", client="c") is not None


async def test_each_side_has_a_bounded_number_of_candidates(rtc, monkeypatch):
    monkeypatch.setattr(rtc_transport, "MAX_ICE_CANDIDATES_PER_SIDE", 2)
    sid = await rtc.offer("inst-a", "sdp", client="c")
    assert await rtc.ice_candidate(sid, {"candidate": "1"}) is True
    assert await rtc.ice_candidate(sid, {"candidate": "2"}) is True
    assert await rtc.ice_candidate(sid, {"candidate": "3"}) is False
    assert rtc.count_viewer_ice(sid) is True
    assert rtc.count_viewer_ice(sid) is True
    assert rtc.count_viewer_ice(sid) is False
    assert len(rtc.get_session(sid).ice_candidates) == 2


async def test_counting_viewer_ice_of_an_unknown_session_raises(rtc):
    with pytest.raises(KeyError):
        rtc.count_viewer_ice("missing")


# ─── Anonymous slots (#1107 residual: one source can't fill them) ─────────


@pytest.mark.security
async def test_one_client_holds_a_bounded_number_of_public_sessions(rtc):
    for _ in range(rtc_transport.MAX_SESSIONS_PER_CLIENT):
        assert await rtc.offer("author", "sdp", client="1.2.3.4") is not None
    assert await rtc.offer("author", "sdp", client="1.2.3.4") is None
    # Another address still gets one.
    assert await rtc.offer("author", "sdp", client="5.6.7.8") is not None


@pytest.mark.security
async def test_one_link_holds_a_bounded_number_of_public_sessions(rtc):
    for i in range(rtc_transport.MAX_SESSIONS_PER_AUTHOR):
        assert await rtc.offer("author", "sdp", client=f"10.0.{i}.1") is not None
    assert await rtc.offer("author", "sdp", client="10.9.9.9") is None
    assert await rtc.offer("other-author", "sdp", client="10.9.9.9") is not None


async def test_an_expired_public_session_frees_its_clients_slot(rtc):
    for _ in range(rtc_transport.MAX_SESSIONS_PER_CLIENT):
        await rtc.offer("author", "sdp", client="1.2.3.4")
    for session in list(rtc._sessions.values()):  # type: ignore[attr-defined]
        session.created_at -= rtc_transport.SESSION_TTL_SECONDS + 1
    assert await rtc.offer("author", "sdp", client="1.2.3.4") is not None


def test_the_per_source_caps_are_below_the_whole_pool():
    assert rtc_transport.MAX_SESSIONS_PER_CLIENT < rtc_transport.MAX_SESSIONS_PER_AUTHOR
    assert rtc_transport.MAX_SESSIONS_PER_AUTHOR < rtc_transport.MAX_LIVE_SESSIONS
