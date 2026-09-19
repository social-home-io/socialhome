"""Tests for SyncSessionManager (§25.6.2 audit fixes S-6/7/8/12/15/16/17)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

from socialhome.domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.sync_manager import (
    ALLOWED_RESOURCES,
    MAX_ACTIVE_SESSIONS_PER_INSTANCE,
    MAX_PENDING_REQUESTS,
    PENDING_REQUEST_TTL_SECONDS,
    PendingSyncRequest,
    MAX_INSTANCE_SYNC_STATUS_SPACES,
    SYNC_BEGIN_RATE_LIMIT_PER_HOUR,
    SyncSessionManager,
    new_sync_id,
)


# ─── Fakes ────────────────────────────────────────────────────────────────


class _FakeFedRepo:
    def __init__(self) -> None:
        self.instances: dict[str, RemoteInstance] = {}

    async def get_instance(self, iid: str):
        return self.instances.get(iid)


def _make_remote(
    iid: str, status: PairingStatus = PairingStatus.CONFIRMED
) -> RemoteInstance:
    return RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh",
        status=status,
        source=InstanceSource.MANUAL,
    )


# ─── new_sync_id ──────────────────────────────────────────────────────────


def test_new_sync_id_high_entropy_s2():
    """S-2: 128-bit URL-safe token. Should not collide in 1000 samples."""
    samples = {new_sync_id() for _ in range(1000)}
    assert len(samples) == 1000


# ─── S-6 rate limit ──────────────────────────────────────────────────────


def test_rate_limit_allows_first_n_then_blocks():
    """S-6 part 1: 5 SPACE_SYNC_BEGIN per (instance, space) per hour."""
    mgr = SyncSessionManager(_FakeFedRepo())
    for i in range(SYNC_BEGIN_RATE_LIMIT_PER_HOUR):
        assert mgr.check_sync_begin_rate("alice", "sp-1", now=100 + i) is True
    # Sixth attempt within the hour is blocked.
    assert mgr.check_sync_begin_rate("alice", "sp-1", now=110) is False


def test_rate_limit_per_space_isolated():
    mgr = SyncSessionManager(_FakeFedRepo())
    for i in range(SYNC_BEGIN_RATE_LIMIT_PER_HOUR):
        assert mgr.check_sync_begin_rate("alice", "sp-1", now=100 + i) is True
    # Different space — full quota available.
    assert mgr.check_sync_begin_rate("alice", "sp-2", now=110) is True


def test_rate_limit_per_instance_isolated():
    mgr = SyncSessionManager(_FakeFedRepo())
    for i in range(SYNC_BEGIN_RATE_LIMIT_PER_HOUR):
        assert mgr.check_sync_begin_rate("alice", "sp-1", now=100 + i) is True
    assert mgr.check_sync_begin_rate("bob", "sp-1", now=110) is True


def test_rate_limit_window_slides_after_3600_s():
    """Once the bucket entries are >1h old, new attempts are accepted again."""
    mgr = SyncSessionManager(_FakeFedRepo())
    for i in range(SYNC_BEGIN_RATE_LIMIT_PER_HOUR):
        assert mgr.check_sync_begin_rate("alice", "sp-1", now=100 + i) is True
    # 4000 s later — bucket entries pruned.
    assert mgr.check_sync_begin_rate("alice", "sp-1", now=4100) is True


# ─── S-7 ICE candidate validation ─────────────────────────────────────────


def test_ice_candidate_must_start_with_candidate_prefix_s7():
    assert (
        SyncSessionManager.validate_ice_candidate(
            "candidate:1 1 UDP 2 1.2.3.4 1234 typ host"
        )
        is True
    )
    assert SyncSessionManager.validate_ice_candidate("not a candidate") is False
    assert SyncSessionManager.validate_ice_candidate("") is False


def test_ice_candidate_size_capped_at_2kb_s7():
    big = "candidate:" + ("x" * 2050)
    assert SyncSessionManager.validate_ice_candidate(big) is False


def test_ice_candidate_rejects_non_string():
    assert SyncSessionManager.validate_ice_candidate(b"bytes-not-str") is False  # type: ignore[arg-type]


# ─── S-12 request_more bounds ─────────────────────────────────────────────


async def test_clamp_request_more_drops_unknown_resource_s12():
    mgr = SyncSessionManager(_FakeFedRepo())
    out = await mgr.clamp_request_more(
        {
            "space_id": "sp-1",
            "resource": "secret_admin_dump",
            "before_seq": 5,
        }
    )
    assert out is None


async def test_clamp_request_more_clamps_before_seq_s12():
    async def get_max_seq(space_id):
        return 100

    mgr = SyncSessionManager(_FakeFedRepo(), get_max_seq=get_max_seq)
    out = await mgr.clamp_request_more(
        {
            "space_id": "sp-1",
            "resource": "posts",
            "before_seq": 10**9,
            "limit": 75,
        }
    )
    assert out is not None
    assert out["before_seq"] == 100
    assert out["limit"] == 75


async def test_clamp_request_more_clamps_limit_to_200():
    mgr = SyncSessionManager(_FakeFedRepo())
    out = await mgr.clamp_request_more(
        {
            "space_id": "sp-1",
            "resource": "posts",
            "limit": 9999,
        }
    )
    assert out is not None
    assert out["limit"] == 200


async def test_clamp_request_more_rejects_missing_space_id():
    mgr = SyncSessionManager(_FakeFedRepo())
    assert await mgr.clamp_request_more({"resource": "posts"}) is None


async def test_clamp_request_more_handles_bad_int():
    mgr = SyncSessionManager(_FakeFedRepo())
    assert (
        await mgr.clamp_request_more(
            {
                "space_id": "sp-1",
                "resource": "posts",
                "before_seq": "not-a-number",
            }
        )
        is None
    )


def test_allowed_resources_locked_down():
    """The allowlist should be small and explicit (S-12)."""
    assert "posts" in ALLOWED_RESOURCES
    assert "page_body" in ALLOWED_RESOURCES
    assert "everything" not in ALLOWED_RESOURCES


# ─── S-6 part 2 + S-8: session admission ──────────────────────────────────


async def test_begin_session_blocks_when_rate_limited_s6():
    """5 SPACE_SYNC_BEGIN per (instance, space) per hour, then DIRECT_FAILED."""
    mgr = SyncSessionManager(_FakeFedRepo())
    sids: list[str] = []
    for _ in range(SYNC_BEGIN_RATE_LIMIT_PER_HOUR):
        sid = new_sync_id()
        d = await mgr.begin_session(
            sync_id=sid,
            space_id="sp-1",
            requester_instance_id="alice",
            provider_instance_id="me",
        )
        assert d.accepted is True
        # Close before next begin so the concurrent-session cap doesn't trip first.
        mgr.close_session(sid)
        sids.append(sid)

    decision = await mgr.begin_session(
        sync_id=new_sync_id(),
        space_id="sp-1",
        requester_instance_id="alice",
        provider_instance_id="me",
    )
    assert decision.accepted is False
    assert decision.reason == "rate_limited"
    assert decision.next_event == FederationEventType.SPACE_SYNC_DIRECT_FAILED
    assert decision.next_payload["reason"] == "rate_limited"


async def test_begin_session_blocks_at_concurrent_cap_s6():
    """3 active sessions per instance."""
    mgr = SyncSessionManager(_FakeFedRepo())
    for i in range(MAX_ACTIVE_SESSIONS_PER_INSTANCE):
        d = await mgr.begin_session(
            sync_id=f"s{i}",
            space_id=f"sp-{i}",
            requester_instance_id="alice",
            provider_instance_id="me",
        )
        assert d.accepted, f"session {i} should be accepted"
    blocked = await mgr.begin_session(
        sync_id="s99",
        space_id="sp-99",
        requester_instance_id="alice",
        provider_instance_id="me",
    )
    assert blocked.accepted is False
    assert blocked.reason == "too_many_sessions"


async def test_begin_session_threads_ice_servers_with_turn():
    """The same ``ice_servers`` list the federation transport uses
    (built via :func:`socialhome.webrtc_ice.build_ice_servers` →
    STUN entry + optional TURN entry) MUST also flow into the
    ``SyncRtcSession`` for §25.6 syncs. Pascal asked: "is sync
    also using TURN as STUN fallback". Yes — provided the operator
    has ``webrtc_turn_url`` set in config, the sync layer picks it
    up automatically because it shares the federation service's
    ``_ice_servers`` slot."""
    mgr = SyncSessionManager(_FakeFedRepo())
    turn_servers = [
        {"urls": ["stun:stun.l.google.com:19302"]},
        {
            "urls": ["turn:turn.example.com:3478"],
            "username": "u",
            "credential": "c",
        },
    ]
    d = await mgr.begin_session(
        sync_id="s-turn",
        space_id="sp-1",
        requester_instance_id="alice",
        provider_instance_id="me",
        ice_servers=turn_servers,
    )
    assert d.accepted is True
    record = mgr.get_session("s-turn")
    assert record is not None
    assert record.rtc is not None
    # ``SyncRtcSession`` stores the list verbatim and feeds it into
    # ``_build_rtc_config`` — no filtering, so a TURN entry survives.
    assert record.rtc._ice_servers == turn_servers


async def test_begin_session_silently_drops_non_member_s1():
    """S-1: a non-member's request is silently dropped (no response event)."""

    async def check_member(space_id, instance_id):
        return False

    mgr = SyncSessionManager(_FakeFedRepo(), check_member=check_member)
    d = await mgr.begin_session(
        sync_id="s1",
        space_id="sp-1",
        requester_instance_id="hostile",
        provider_instance_id="me",
    )
    assert d.accepted is False
    assert d.reason == "not_a_member"
    assert d.next_event is None


async def test_begin_session_rejects_non_member_dissolved_with_reason():
    """S-1 backstop: a non-member with a ``reject_reason`` classifier
    returning ``"dissolved"`` gets a SPACE_SYNC_REJECTED follow-up so
    the member can archive its orphaned stub."""

    async def check_member(space_id, instance_id):
        return False

    async def reject_reason(space_id):
        return "dissolved"

    mgr = SyncSessionManager(
        _FakeFedRepo(),
        check_member=check_member,
        reject_reason=reject_reason,
    )
    d = await mgr.begin_session(
        sync_id="sid-1",
        space_id="sp-1",
        requester_instance_id="ex-member",
        provider_instance_id="me",
    )
    assert d.accepted is False
    assert d.reason == "not_a_member"
    assert d.next_event is FederationEventType.SPACE_SYNC_REJECTED
    assert d.next_payload == {
        "sync_id": "sid-1",
        "space_id": "sp-1",
        "reason": "dissolved",
    }


async def test_begin_session_rejects_non_member_removed_with_reason():
    """Same backstop, but the space row still exists → ``"removed"``."""

    async def check_member(space_id, instance_id):
        return False

    async def reject_reason(space_id):
        return "removed"

    mgr = SyncSessionManager(
        _FakeFedRepo(),
        check_member=check_member,
        reject_reason=reject_reason,
    )
    d = await mgr.begin_session(
        sync_id="sid-2",
        space_id="sp-2",
        requester_instance_id="ex-member",
        provider_instance_id="me",
    )
    assert d.accepted is False
    assert d.next_event is FederationEventType.SPACE_SYNC_REJECTED
    assert d.next_payload["reason"] == "removed"
    assert d.next_payload["sync_id"] == "sid-2"
    assert d.next_payload["space_id"] == "sp-2"


async def test_begin_session_non_member_silent_without_classifier():
    """Backward compat: ``check_member`` but NO ``reject_reason`` keeps
    the original S-1 silent drop (no follow-up event)."""

    async def check_member(space_id, instance_id):
        return False

    mgr = SyncSessionManager(_FakeFedRepo(), check_member=check_member)
    d = await mgr.begin_session(
        sync_id="sid-3",
        space_id="sp-3",
        requester_instance_id="hostile",
        provider_instance_id="me",
    )
    assert d.accepted is False
    assert d.reason == "not_a_member"
    assert d.next_event is None


async def test_begin_session_rate_limit_fires_before_member_check():
    """S-6 anti-probe: a rate-limited non-member gets
    SPACE_SYNC_DIRECT_FAILED (rate_limited), never SPACE_SYNC_REJECTED —
    the rate check runs BEFORE the membership classifier."""

    async def check_member(space_id, instance_id):
        return False

    async def reject_reason(space_id):
        return "removed"

    mgr = SyncSessionManager(
        _FakeFedRepo(),
        check_member=check_member,
        reject_reason=reject_reason,
    )
    # Burn the per-(instance, space) hourly bucket.
    for _ in range(SYNC_BEGIN_RATE_LIMIT_PER_HOUR):
        sid = new_sync_id()
        await mgr.begin_session(
            sync_id=sid,
            space_id="sp-rl",
            requester_instance_id="prober",
            provider_instance_id="me",
        )
        mgr.close_session(sid)

    blocked = await mgr.begin_session(
        sync_id=new_sync_id(),
        space_id="sp-rl",
        requester_instance_id="prober",
        provider_instance_id="me",
    )
    assert blocked.accepted is False
    assert blocked.reason == "rate_limited"
    assert blocked.next_event is FederationEventType.SPACE_SYNC_DIRECT_FAILED
    assert blocked.next_payload["reason"] == "rate_limited"


async def test_apply_answer_rejects_wrong_origin_s14():
    """S-14: the answer must come from the original requester."""
    mgr = SyncSessionManager(_FakeFedRepo())
    await mgr.begin_session(
        sync_id="s1",
        space_id="sp-1",
        requester_instance_id="alice",
        provider_instance_id="me",
    )
    ok = await mgr.apply_answer(
        sync_id="s1", sdp_answer="v=0\r\n", from_instance="hostile"
    )
    assert ok is False


async def test_apply_answer_accepts_correct_origin_s14():
    mgr = SyncSessionManager(_FakeFedRepo())
    await mgr.begin_session(
        sync_id="s1",
        space_id="sp-1",
        requester_instance_id="alice",
        provider_instance_id="me",
    )
    ok = await mgr.apply_answer(
        sync_id="s1", sdp_answer="v=0\r\n", from_instance="alice"
    )
    assert ok is True


# ─── S-15: relay fallback ─────────────────────────────────────────────────


async def test_trigger_relay_sync_returns_new_begin_for_initial_mode_s15():
    mgr = SyncSessionManager(_FakeFedRepo())
    await mgr.begin_session(
        sync_id="s1",
        space_id="sp-1",
        requester_instance_id="alice",
        provider_instance_id="me",
        sync_mode="initial",
    )
    decision = await mgr.trigger_relay_sync("s1")
    assert decision.accepted is True
    assert decision.next_event == FederationEventType.SPACE_SYNC_BEGIN
    assert decision.next_payload["prefer_direct"] is False
    assert decision.next_payload["space_id"] == "sp-1"


async def test_trigger_relay_sync_aborts_tier3_per_25_8_18():
    """§25.8.18: full sync MUST NOT fall back to relay."""
    mgr = SyncSessionManager(_FakeFedRepo())
    await mgr.begin_session(
        sync_id="s1",
        space_id="sp-1",
        requester_instance_id="alice",
        provider_instance_id="me",
        sync_mode="full",
    )
    decision = await mgr.trigger_relay_sync("s1")
    assert decision.accepted is False
    assert decision.reason == "tier3_abort"


async def test_trigger_relay_sync_with_unknown_id():
    mgr = SyncSessionManager(_FakeFedRepo())
    decision = await mgr.trigger_relay_sync("nope")
    assert decision.accepted is False


# ─── S-17: instance_sync_status guard ─────────────────────────────────────


async def test_instance_sync_status_rejects_unknown_sender_s17():
    mgr = SyncSessionManager(_FakeFedRepo())
    spaces = await mgr.validate_instance_sync_status(
        from_instance="hostile",
        payload={"spaces": ["sp-1", "sp-2"]},
    )
    assert spaces == []


async def test_instance_sync_status_rejects_pending_pair_s17():
    repo = _FakeFedRepo()
    repo.instances["alice"] = _make_remote("alice", PairingStatus.PENDING_RECEIVED)
    mgr = SyncSessionManager(repo)
    spaces = await mgr.validate_instance_sync_status(
        from_instance="alice",
        payload={"spaces": ["sp-1"]},
    )
    assert spaces == []


async def test_instance_sync_status_caps_space_count_s17():
    repo = _FakeFedRepo()
    repo.instances["alice"] = _make_remote("alice")
    mgr = SyncSessionManager(repo)
    huge = [f"sp-{i}" for i in range(MAX_INSTANCE_SYNC_STATUS_SPACES + 1)]
    spaces = await mgr.validate_instance_sync_status(
        from_instance="alice",
        payload={"spaces": huge},
    )
    assert spaces == []


async def test_instance_sync_status_accepts_known_active_peer():
    repo = _FakeFedRepo()
    repo.instances["alice"] = _make_remote("alice")
    mgr = SyncSessionManager(repo)
    spaces = await mgr.validate_instance_sync_status(
        from_instance="alice",
        payload={"spaces": ["sp-1", "sp-2"]},
    )
    assert spaces == ["sp-1", "sp-2"]


async def test_instance_sync_status_extracts_space_ids_from_dicts():
    repo = _FakeFedRepo()
    repo.instances["alice"] = _make_remote("alice")
    mgr = SyncSessionManager(repo)
    spaces = await mgr.validate_instance_sync_status(
        from_instance="alice",
        payload={"spaces": [{"space_id": "sp-1"}, {"space_id": "sp-2"}, {"junk": "x"}]},
    )
    assert spaces == ["sp-1", "sp-2"]


# ─── register_requester_https_session (HTTPS/mesh receive session) ─────────


def test_register_requester_https_session_creates_https_receive_session():
    """A mesh requester registers an HTTPS receive-session up front so the
    inbound SPACE_SYNC_CHUNK handler finds it instead of dropping chunks."""
    mgr = SyncSessionManager(_FakeFedRepo())
    ok = mgr.register_requester_https_session(
        sync_id="sync-1",
        space_id="sp-1",
        requester_instance_id="me",
        provider_instance_id="host",
    )
    assert ok is True
    rec = mgr.get_session("sync-1")
    assert rec is not None
    assert rec.transport_mode == "https"
    assert rec.provider_instance_id == "host"
    assert rec.requester_instance_id == "me"
    assert rec.space_id == "sp-1"
    assert rec.sync_mode == "initial"
    assert rec.rtc is None
    assert rec.created_at > 0


def test_register_requester_https_session_does_not_clobber_existing():
    """Second call with the same sync_id returns False and keeps the first
    record intact."""
    mgr = SyncSessionManager(_FakeFedRepo())
    first = mgr.register_requester_https_session(
        sync_id="sync-1",
        space_id="sp-1",
        requester_instance_id="me",
        provider_instance_id="host",
    )
    assert first is True
    again = mgr.register_requester_https_session(
        sync_id="sync-1",
        space_id="sp-OTHER",
        requester_instance_id="someone-else",
        provider_instance_id="other-host",
    )
    assert again is False
    rec = mgr.get_session("sync-1")
    assert rec is not None
    # Unchanged from the first registration.
    assert rec.space_id == "sp-1"
    assert rec.provider_instance_id == "host"
    assert rec.requester_instance_id == "me"


def test_register_requester_https_session_fresh_id_returns_true():
    mgr = SyncSessionManager(_FakeFedRepo())
    assert (
        mgr.register_requester_https_session(
            sync_id="fresh",
            space_id="sp-1",
            requester_instance_id="me",
            provider_instance_id="host",
        )
        is True
    )


# ─── reap_stale (TTL backstop for leaked sessions) ─────────────────────────


def test_reap_stale_closes_session_older_than_ttl():
    """A session whose created_at predates the TTL is reaped."""
    mgr = SyncSessionManager(_FakeFedRepo())
    mgr.register_requester_https_session(
        sync_id="old",
        space_id="sp-1",
        requester_instance_id="me",
        provider_instance_id="host",
    )
    rec = mgr.get_session("old")
    assert rec is not None
    ttl = 1800.0
    created = time.time() - ttl - 1
    rec.created_at = created
    n = mgr.reap_stale(ttl)
    assert n == 1
    assert mgr.get_session("old") is None


def test_reap_stale_keeps_fresh_session_within_ttl():
    """A session created within the TTL window survives the reap."""
    mgr = SyncSessionManager(_FakeFedRepo())
    mgr.register_requester_https_session(
        sync_id="fresh",
        space_id="sp-1",
        requester_instance_id="me",
        provider_instance_id="host",
    )
    n = mgr.reap_stale(1800.0)
    assert n == 0
    assert mgr.get_session("fresh") is not None


def test_reap_stale_tears_down_rtc_handle_via_close_session():
    """Reaping goes through close_session, so an rtc handle is closed
    (not just dropped from the dict)."""
    mgr = SyncSessionManager(_FakeFedRepo())
    mgr.register_requester_https_session(
        sync_id="with-rtc",
        space_id="sp-1",
        requester_instance_id="me",
        provider_instance_id="host",
    )
    rec = mgr.get_session("with-rtc")
    assert rec is not None
    fake_rtc = MagicMock()
    rec.rtc = fake_rtc
    n = mgr.reap_stale(1800.0, now=rec.created_at + 1800.0 + 1)
    assert n == 1
    fake_rtc.close.assert_called_once()
    assert mgr.get_session("with-rtc") is None


# ─── F3: only a sync WE asked for may be offered to us ────────────────


def _mgr():
    return SyncSessionManager(_FakeFedRepo())


def test_pending_sync_request_is_unknown_until_we_record_one():
    mgr = _mgr()
    assert mgr.pending_sync_request("s1") is None


def test_record_sync_request_pins_the_provider_and_space():
    """``sync_id`` is the only thing addressing a session, and until now
    the requester kept no record of the ones it issued — so any peer's
    ``SPACE_SYNC_OFFER`` minted one."""
    mgr = _mgr()
    mgr.record_sync_request(sync_id="s1", space_id="sp", provider_instance_id="host")
    pending = mgr.pending_sync_request("s1")
    assert pending is not None
    assert pending.space_id == "sp"
    assert pending.provider_instance_id == "host"


def test_pending_sync_requests_expire():
    """A request the provider never answered must not sit there for the
    process's lifetime waiting to be claimed."""
    mgr = _mgr()
    mgr.record_sync_request(sync_id="s1", space_id="sp", provider_instance_id="host")
    mgr._requests["s1"] = PendingSyncRequest(
        sync_id="s1",
        space_id="sp",
        provider_instance_id="host",
        created_at=time.time() - PENDING_REQUEST_TTL_SECONDS - 1,
    )
    assert mgr.pending_sync_request("s1") is None


def test_pending_sync_requests_are_capped():
    """Bounded like every other in-memory table here — the oldest entries
    are evicted rather than letting a retry storm grow it without end."""
    mgr = _mgr()
    for i in range(MAX_PENDING_REQUESTS + 10):
        mgr.record_sync_request(
            sync_id=f"s{i}", space_id="sp", provider_instance_id="host"
        )
    assert len(mgr._requests) <= MAX_PENDING_REQUESTS
    assert mgr.pending_sync_request(f"s{MAX_PENDING_REQUESTS + 9}") is not None


async def test_apply_offer_records_the_provider_on_a_fresh_session():
    """The requester-side session used to be minted with
    ``provider_instance_id=""`` — the empty string the chunk handler's
    origin pin skipped over."""
    mgr = _mgr()
    with patch(
        "socialhome.federation.sync_manager.SyncRtcSession",
        MagicMock(return_value=MagicMock(create_answer=AsyncMock(return_value="a"))),
    ):
        await mgr.apply_offer(
            sync_id="s1",
            sdp_offer="o",
            requester_instance_id="us",
            provider_instance_id="host",
            space_id="sp",
        )
    assert mgr.get_session("s1").provider_instance_id == "host"
