"""Federated-path coverage for CallSignalingService (spec §26)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from socialhome.crypto import generate_identity_keypair
from socialhome.domain.federation import FederationEventType
from socialhome.federation.sdp_signing import (
    sign_rtc_offer,
    signed_sdp_to_dict,
)
from socialhome.services.call_service import (
    MAX_CALLS_PER_USER,
    RINGING_TTL_SECONDS,
    CallAlreadyAnsweredError,
)

from ._call_fakes import FakeFederation, make_call_service


class _Event:
    def __init__(self, et, from_inst, payload):
        self.event_type = et
        self.from_instance = from_inst
        self.payload = payload


def _federated_env(*, federation: FakeFederation | None = None):
    """Set up bob locally + alice remote on ``conv-ab``."""
    env = make_call_service(federation=federation)
    env.users.add_user("bob", "uid-bob")
    env.users.add_remote(
        user_id="uid-alice",
        instance_id="remote-inst",
        remote_username="alice",
    )
    env.convos.add_conversation(
        "conv-ab",
        ["bob"],
        remotes=[("remote-inst", "alice", "uid-alice")],
    )
    return env


# ─── Federated answer / ICE / hangup paths ────────────────────────────────


async def test_federated_answer_sends_call_answer_back_to_caller():
    """Bob (local) answers a federated incoming call; answer is relayed."""
    env = _federated_env()
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_OFFER,
            "remote-inst",
            {
                "call_id": "c1",
                "conversation_id": "conv-ab",
                "from_user": "uid-alice",
                "to_user": "uid-bob",
                "call_type": "audio",
            },
        )
    )
    await env.svc.answer_call(
        call_id="c1",
        answerer_user_id="uid-bob",
        sdp_answer="v=0\r\nans\r\n",
    )
    answers = [s for s in env.fed.sent if s[1] == FederationEventType.CALL_ANSWER]
    assert answers and answers[0][0] == "remote-inst"


async def test_federated_ice_candidate_routes_to_remote_caller():
    env = _federated_env()
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_OFFER,
            "remote-inst",
            {
                "call_id": "c1",
                "conversation_id": "conv-ab",
                "from_user": "uid-alice",
                "to_user": "uid-bob",
                "call_type": "audio",
            },
        )
    )
    await env.svc.add_ice_candidate(
        call_id="c1",
        from_user_id="uid-bob",
        candidate={"candidate": "x", "sdpMid": "0"},
    )
    ice = [s for s in env.fed.sent if s[1] == FederationEventType.CALL_ICE_CANDIDATE]
    assert ice and ice[0][0] == "remote-inst"


async def test_federated_hangup_sends_remote_call_hangup():
    env = _federated_env()
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_OFFER,
            "remote-inst",
            {
                "call_id": "c1",
                "conversation_id": "conv-ab",
                "from_user": "uid-alice",
                "to_user": "uid-bob",
                "call_type": "audio",
            },
        )
    )
    await env.svc.hangup(call_id="c1", hanger_user_id="uid-bob")
    hangups = [s for s in env.fed.sent if s[1] == FederationEventType.CALL_HANGUP]
    assert hangups and hangups[0][0] == "remote-inst"


# ─── handle_federated_signal — additional events ─────────────────────────


async def test_handle_call_decline_cleans_record():
    env = make_call_service()
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_OFFER,
            "remote-inst",
            {
                "call_id": "c1",
                "conversation_id": "conv-ab",
                "from_user": "uid-alice",
                "to_user": "uid-bob",
                "call_type": "audio",
            },
        )
    )
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_DECLINE,
            "remote-inst",
            {"call_id": "c1", "hanger_user": "uid-bob"},
        )
    )
    assert env.svc.get_call("c1") is None


async def test_handle_call_busy_cleans_record():
    env = make_call_service()
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_OFFER,
            "remote-inst",
            {
                "call_id": "c1",
                "conversation_id": "conv-ab",
                "from_user": "uid-alice",
                "to_user": "uid-bob",
                "call_type": "audio",
            },
        )
    )
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_BUSY,
            "remote-inst",
            {"call_id": "c1", "hanger_user": "uid-bob"},
        )
    )
    assert env.svc.get_call("c1") is None


async def test_handle_call_quality_persists_without_record():
    """CALL_QUALITY from a participant's household persists even after the
    routing record is gone (only the persisted row is left)."""
    env = _caller_env()
    cid = await _start_outbound(env)
    env.svc._calls.clear()
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_QUALITY,
            "remote-inst",
            {
                "call_id": cid,
                "rtt_ms": 42,
                "reporter_user": "uid-bob",
                "sampled_at": 1700000000,
            },
        )
    )
    samples = await env.call_repo.list_quality_samples(cid)
    assert samples and samples[0].rtt_ms == 42


@pytest.mark.parametrize(
    ("reporter", "sender"),
    [
        pytest.param("uid-alice", "remote-inst", id="a local participant"),
        pytest.param("uid-bob", "stranger-inst", id="another household"),
        pytest.param("uid-carol", "remote-inst", id="not a participant"),
    ],
)
async def test_call_quality_names_only_the_senders_own_participant(reporter, sender):
    env = _caller_env()
    cid = await _start_outbound(env)
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_QUALITY,
            sender,
            {"call_id": cid, "rtt_ms": 1, "reporter_user": reporter},
        )
    )
    assert await env.call_repo.list_quality_samples(cid) == []


@pytest.mark.parametrize(
    "ender", [None, "uid-alice", "uid-carol"], ids=["missing", "local", "outsider"]
)
async def test_remote_hangup_names_the_senders_own_participant(ender):
    env = _caller_env()
    cid = await _start_outbound(env)
    payload = {"call_id": cid}
    if ender is not None:
        payload["hanger_user"] = ender
    await env.svc.handle_federated_signal(
        _Event(FederationEventType.CALL_HANGUP, "remote-inst", payload)
    )
    assert (await env.call_repo.get_call(cid)).status == "ringing"


# ─── Signed SDP verification path ─────────────────────────────────────────


async def test_handle_call_offer_with_signed_sdp_passes_verification():
    """When sender's pk is known, SDP signature is verified before forwarding."""
    sender_kp = generate_identity_keypair()
    env = _federated_env(
        federation=FakeFederation(peer_pk_hex=sender_kp.public_key.hex()),
    )
    signed = sign_rtc_offer(
        "v=0\r\n",
        "offer",
        identity_seed=sender_kp.private_key,
    )
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_OFFER,
            "remote-inst",
            {
                "call_id": "c1",
                "conversation_id": "conv-ab",
                "from_user": "uid-alice",
                "to_user": "uid-bob",
                "call_type": "audio",
                "signed_sdp": signed_sdp_to_dict(signed),
            },
        )
    )
    assert env.svc.get_call("c1") is not None
    assert any(c[1].get("type") == "call.ringing" for c in env.ws.calls)


async def test_handle_call_offer_with_bad_signed_sdp_drops_call():
    sender_kp = generate_identity_keypair()
    other_kp = generate_identity_keypair()  # wrong key
    env = make_call_service(
        federation=FakeFederation(peer_pk_hex=other_kp.public_key.hex()),
    )
    signed = sign_rtc_offer(
        "v=0\r\n",
        "offer",
        identity_seed=sender_kp.private_key,
    )
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_OFFER,
            "remote-inst",
            {
                "call_id": "c1",
                "conversation_id": "conv-ab",
                "from_user": "uid-alice",
                "to_user": "uid-bob",
                "call_type": "audio",
                "signed_sdp": signed_sdp_to_dict(signed),
            },
        )
    )
    assert env.svc.get_call("c1") is None


# ─── Constants ────────────────────────────────────────────────────────────


def test_ringing_ttl_constant_matches_spec():
    """§26.8 — 90 s ringing TTL."""
    assert RINGING_TTL_SECONDS == 90


# ─── Callee-side persistence (routes guard on the persisted row) ─────────


async def test_federated_offer_persists_ringing_row_for_callee_routes():
    """Every per-call route on the callee's household (answer / ice /
    decline / hangup) authorises against the persisted ``call_sessions``
    row. An inbound ``CALL_OFFER`` that only filled the in-memory record
    made all of them 404, so a cross-household call could ring but never
    be answered."""
    env = _federated_env()
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_OFFER,
            "remote-inst",
            {
                "call_id": "c1",
                "conversation_id": "conv-ab",
                "from_user": "uid-alice",
                "to_user": "uid-bob",
                "call_type": "video",
            },
        )
    )
    row = await env.call_repo.get_call("c1")
    assert row is not None
    assert row.status == "ringing"
    assert row.conversation_id == "conv-ab"
    assert row.initiator_user_id == "uid-alice"
    assert row.callee_user_id == "uid-bob"
    assert row.call_type == "video"
    assert set(row.participant_user_ids) == {"uid-alice", "uid-bob"}

    await env.svc.answer_call(
        call_id="c1",
        answerer_user_id="uid-bob",
        sdp_answer="v=0\r\nans\r\n",
    )
    assert (await env.call_repo.get_call("c1")).status == "active"


async def test_federated_offer_for_non_local_callee_is_dropped():
    """A peer can't plant call rows (or ring) for a user this household
    doesn't host — the offer is dropped before anything is stored."""
    env = _federated_env()
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_OFFER,
            "remote-inst",
            {
                "call_id": "c-stray",
                "conversation_id": "conv-ab",
                "from_user": "uid-alice",
                "to_user": "uid-nobody",
                "call_type": "audio",
            },
        )
    )
    assert await env.call_repo.get_call("c-stray") is None
    assert env.svc.get_call("c-stray") is None
    assert not any(p.get("call_id") == "c-stray" for _u, p in env.ws.calls)


@pytest.mark.parametrize(
    ("overrides", "from_instance"),
    [
        ({"call_type": "screen"}, "remote-inst"),
        ({"conversation_id": ""}, "remote-inst"),
        ({"conversation_id": "conv-unknown"}, "remote-inst"),
        ({}, "other-inst"),
    ],
    ids=["bad-call-type", "no-conversation", "callee-not-member", "foreign-sender"],
)
async def test_federated_offer_outside_a_shared_conversation_is_dropped(
    overrides, from_instance
):
    env = _federated_env()
    payload = {
        "call_id": "c-x",
        "conversation_id": "conv-ab",
        "from_user": "uid-alice",
        "to_user": "uid-bob",
        "call_type": "audio",
        **overrides,
    }
    await env.svc.handle_federated_signal(
        _Event(FederationEventType.CALL_OFFER, from_instance, payload)
    )
    assert await env.call_repo.get_call("c-x") is None
    assert env.svc.get_call("c-x") is None
    assert env.ws.calls == []


async def test_federated_offer_with_spoofed_caller_is_dropped():
    """The sending household may only place calls as one of its own users —
    not as a local user or a user of another household."""
    env = _federated_env()
    env.users.add_user("carol", "uid-carol")  # local
    for spoofed in ("uid-carol", "uid-unknown"):
        await env.svc.handle_federated_signal(
            _Event(
                FederationEventType.CALL_OFFER,
                "remote-inst",
                {
                    "call_id": f"c-{spoofed}",
                    "conversation_id": "conv-ab",
                    "from_user": spoofed,
                    "to_user": "uid-bob",
                    "call_type": "audio",
                },
            )
        )
        assert await env.call_repo.get_call(f"c-{spoofed}") is None
    assert env.ws.calls == []


def _offer(call_id="c1", **extra):
    return _Event(
        FederationEventType.CALL_OFFER,
        "remote-inst",
        {
            "call_id": call_id,
            "conversation_id": "conv-ab",
            "from_user": "uid-alice",
            "to_user": "uid-bob",
            "call_type": "audio",
            **extra,
        },
    )


async def test_repeat_offer_never_resets_a_live_call():
    """A second ``CALL_OFFER`` for the same call_id (late join, retransmit,
    or a hostile peer) merges into the call instead of resetting it."""
    env = _federated_env()
    await env.svc.handle_federated_signal(_offer())
    await env.svc.answer_call(
        call_id="c1", answerer_user_id="uid-bob", sdp_answer="v=0\r\n"
    )
    connected_at = (await env.call_repo.get_call("c1")).connected_at
    env.ws.calls.clear()

    await env.svc.handle_federated_signal(_offer(late_join=True))
    row = await env.call_repo.get_call("c1")
    assert row.status == "active"
    assert row.connected_at == connected_at
    assert env.svc.get_call("c1").status == "in_progress"
    assert [p["type"] for _u, p in env.ws.calls] == ["call.peer_join"]


async def test_repeat_offer_for_another_conversation_is_dropped():
    env = _federated_env()
    env.convos.add_conversation(
        "conv-other", ["bob"], remotes=[("remote-inst", "alice", "uid-alice")]
    )
    await env.svc.handle_federated_signal(_offer())
    env.ws.calls.clear()
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_OFFER,
            "remote-inst",
            {
                "call_id": "c1",
                "conversation_id": "conv-other",
                "from_user": "uid-alice",
                "to_user": "uid-bob",
                "call_type": "audio",
            },
        )
    )
    assert (await env.call_repo.get_call("c1")).conversation_id == "conv-ab"
    assert env.ws.calls == []


async def test_answering_twice_is_refused():
    """Only the first answer wins; the answerer's other devices are told
    to stop ringing."""
    env = _federated_env()
    await env.svc.handle_federated_signal(_offer())
    await env.svc.answer_call(
        call_id="c1", answerer_user_id="uid-bob", sdp_answer="v=0\r\n"
    )
    assert (
        "uid-bob",
        {"type": "call.answered", "call_id": "c1"},
    ) in env.ws.calls
    with pytest.raises(CallAlreadyAnsweredError):
        await env.svc.answer_call(
            call_id="c1", answerer_user_id="uid-bob", sdp_answer="v=0\r\n"
        )


# ─── Caller side of a cross-household call ───────────────────────────────


def _caller_env():
    """alice local, calling bob on ``remote-inst``."""
    env = make_call_service()
    env.users.add_user("alice", "uid-alice")
    env.users.add_remote(
        user_id="uid-bob", instance_id="remote-inst", remote_username="bob"
    )
    env.convos.add_conversation(
        "conv-ab", ["alice"], remotes=[("remote-inst", "bob", "uid-bob")]
    )
    return env


async def _start_outbound(env) -> str:
    r = await env.svc.initiate_call(
        caller_user_id="uid-alice",
        conversation_id="conv-ab",
        call_type="audio",
        sdp_offer="v=0\r\n",
    )
    return r["call_id"]


async def test_remote_answer_marks_the_caller_row_active_so_the_sweep_spares_it():
    """Regression: the caller-side row stayed ``ringing`` after the remote
    answer, so the stale-call sweep marked a live call missed at 90 s and
    dropped the routing record mid-call."""
    env = _caller_env()
    cid = await _start_outbound(env)
    await env.svc.handle_federated_signal(
        _Event(FederationEventType.CALL_ANSWER, "remote-inst", {"call_id": cid})
    )
    row = await env.call_repo.get_call(cid)
    assert row.status == "active"
    assert row.connected_at
    # Age the row past the ringing TTL: a live call must survive the sweep.
    env.call_repo._sessions[cid] = replace(row, started_at="2000-01-01T00:00:00")
    assert await env.svc.gc_expired() == 0
    assert env.svc.get_call(cid) is not None


async def test_answer_or_ice_from_an_uninvolved_household_is_ignored():
    env = _caller_env()
    cid = await _start_outbound(env)
    env.ws.calls.clear()
    for et in (FederationEventType.CALL_ANSWER, FederationEventType.CALL_ICE_CANDIDATE):
        await env.svc.handle_federated_signal(
            _Event(et, "stranger-inst", {"call_id": cid, "candidate": {}})
        )
    assert (await env.call_repo.get_call(cid)).status == "ringing"
    assert env.ws.calls == []


@pytest.mark.parametrize(
    ("event_type", "status"),
    [
        (FederationEventType.CALL_HANGUP, "ended"),
        (FederationEventType.CALL_END, "ended"),
        (FederationEventType.CALL_DECLINE, "declined"),
        (FederationEventType.CALL_BUSY, "declined"),
    ],
)
async def test_remote_end_closes_the_caller_row(event_type, status):
    """Regression: terminal inbound events left the row ``ringing`` (later
    reported missed) or ``active`` (a phantom live call)."""
    env = _caller_env()
    cid = await _start_outbound(env)
    await env.svc.handle_federated_signal(
        _Event(event_type, "remote-inst", {"call_id": cid, "hanger_user": "uid-bob"})
    )
    row = await env.call_repo.get_call(cid)
    assert row.status == status
    assert row.ended_at
    assert env.svc.get_call(cid) is None
    assert ("uid-alice", {"type": "call.ended", "call_id": cid}) in env.ws.calls


async def test_remote_hangup_after_answer_records_a_duration():
    env = _caller_env()
    cid = await _start_outbound(env)
    await env.svc.handle_federated_signal(
        _Event(FederationEventType.CALL_ANSWER, "remote-inst", {"call_id": cid})
    )
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_HANGUP,
            "remote-inst",
            {"call_id": cid, "hanger_user": "uid-bob"},
        )
    )
    row = await env.call_repo.get_call(cid)
    assert row.status == "ended"
    assert row.duration_seconds is not None


async def test_remote_end_from_an_uninvolved_household_is_ignored():
    env = _caller_env()
    cid = await _start_outbound(env)
    await env.svc.handle_federated_signal(
        _Event(FederationEventType.CALL_HANGUP, "stranger-inst", {"call_id": cid})
    )
    assert (await env.call_repo.get_call(cid)).status == "ringing"
    assert env.svc.get_call(cid) is not None


async def test_remote_end_closes_the_row_even_without_a_routing_record():
    """After a restart only the persisted row is left; it still closes."""
    env = _caller_env()
    cid = await _start_outbound(env)
    env.svc._calls.clear()
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_HANGUP,
            "remote-inst",
            {"call_id": cid, "hanger_user": "uid-bob"},
        )
    )
    assert (await env.call_repo.get_call(cid)).status == "ended"


async def test_inbound_ringing_is_capped_per_callee():
    env = _federated_env()
    for i in range(MAX_CALLS_PER_USER + 2):
        await env.svc.handle_federated_signal(_offer(call_id=f"c{i}"))
    assert len(env.svc.list_calls_for_user("uid-bob")) == MAX_CALLS_PER_USER
    assert await env.call_repo.get_call(f"c{MAX_CALLS_PER_USER}") is None
