"""Tests for SyncSessionManager (§25.6.2 audit fixes S-6/7/8/12/15/16/17)."""

from __future__ import annotations


from socialhome.domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.sync_manager import (
    ALLOWED_RESOURCES,
    MAX_ACTIVE_SESSIONS_PER_INSTANCE,
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
