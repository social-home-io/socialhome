"""Group-call mesh coverage for CallSignalingService (spec §26.4).

A group call is a full WebRTC mesh: the caller offers to every callee
(one offer each), callees open legs to each other through ``join_call``
and answer those legs with ``answer_call(to_user_id=…)``, ICE is routed
per leg with ``to_user_id``, and hanging up / declining only takes the
leaver out until fewer than two people are left.
"""

from __future__ import annotations

import pytest

from socialhome.domain.federation import FederationEventType
from socialhome.services.call_service import (
    MAX_CALL_PARTICIPANTS,
    CallAlreadyAnsweredError,
    CallTooLargeError,
)

from ._call_fakes import make_call_service

A, B, C = "uid-a", "uid-b", "uid-c"


class _Event:
    def __init__(self, et, from_inst, payload):
        self.event_type = et
        self.from_instance = from_inst
        self.payload = payload


def _env():
    env = make_call_service()
    for name, uid in (("alice", A), ("bob", B), ("carol", C)):
        env.users.add_user(name, uid)
    env.convos.add_conversation("trio", ["alice", "bob", "carol"])
    return env


async def _start(env, offers=None, **kw) -> str:
    r = await env.svc.initiate_call(
        caller_user_id=A,
        conversation_id="trio",
        call_type="video",
        sdp_offers=offers if offers is not None else {B: "OFFER-B", C: "OFFER-C"},
        **kw,
    )
    return r["call_id"]


def _frames(env, user, type_):
    return [p for u, p in env.ws.calls if u == user and p.get("type") == type_]


# ─── ringing ──────────────────────────────────────────────────────────────


async def test_each_callee_rings_with_its_own_offer_and_the_roster():
    env = _env()
    r = await env.svc.initiate_call(
        caller_user_id=A,
        conversation_id="trio",
        call_type="video",
        sdp_offers={B: "OFFER-B", C: "OFFER-C"},
    )
    assert r["participants"] == sorted([A, B, C])
    (rb,) = _frames(env, B, "call.ringing")
    (rc,) = _frames(env, C, "call.ringing")
    assert rb["signed_sdp"]["sdp"] == "OFFER-B"
    assert rc["signed_sdp"]["sdp"] == "OFFER-C"
    assert rb["participants"] == rc["participants"] == sorted([A, B, C])


async def test_a_callee_without_an_offer_falls_back_to_sdp_offer_or_is_not_rung():
    env = _env()
    await _start(env, offers={B: "OFFER-B"}, sdp_offer="SHARED")
    assert _frames(env, C, "call.ringing")[0]["signed_sdp"]["sdp"] == "SHARED"

    env2 = _env()
    cid = await _start(env2, offers={B: "OFFER-B"})
    assert _frames(env2, C, "call.ringing") == []
    assert C not in env2.svc.get_call(cid).participants


async def test_a_call_larger_than_the_mesh_cap_is_refused():
    env = make_call_service()
    names = [f"u{i}" for i in range(MAX_CALL_PARTICIPANTS + 1)]
    for n in names:
        env.users.add_user(n, f"uid-{n}")
    env.convos.add_conversation("big", names)
    with pytest.raises(CallTooLargeError):
        await env.svc.initiate_call(
            caller_user_id="uid-u0",
            conversation_id="big",
            call_type="audio",
            sdp_offer="v=0\r\n",
        )
    assert env.ws.calls == []


# ─── answers ──────────────────────────────────────────────────────────────


async def test_every_callee_answers_the_caller_and_the_caller_learns_who():
    env = _env()
    cid = await _start(env)
    await env.svc.answer_call(call_id=cid, answerer_user_id=B, sdp_answer="ANS-B")
    await env.svc.answer_call(call_id=cid, answerer_user_id=C, sdp_answer="ANS-C")
    got = {
        f["from_user"]: f["signed_sdp"]["sdp"] for f in _frames(env, A, "call.answered")
    }
    assert got == {B: "ANS-B", C: "ANS-C"}
    assert (await env.call_repo.get_call(cid)).status == "active"
    # Another device of bob answering again is refused.
    with pytest.raises(CallAlreadyAnsweredError):
        await env.svc.answer_call(call_id=cid, answerer_user_id=B, sdp_answer="X")


async def test_a_mesh_leg_answer_goes_to_the_offering_callee_only():
    env = _env()
    cid = await _start(env)
    await env.svc.answer_call(call_id=cid, answerer_user_id=B, sdp_answer="ANS-B")
    await env.svc.answer_call(call_id=cid, answerer_user_id=C, sdp_answer="ANS-C")
    # bob (lower id) opens his leg to carol …
    await env.svc.join_call(call_id=cid, joiner_user_id=B, sdp_offers={C: "LEG-BC"})
    (pj,) = _frames(env, C, "call.peer_join")
    assert pj["joiner_user_id"] == B and pj["signed_sdp"]["sdp"] == "LEG-BC"
    # … and carol answers it, addressed to bob.
    env.ws.calls.clear()
    await env.svc.answer_call(
        call_id=cid, answerer_user_id=C, sdp_answer="LEG-ANS", to_user_id=B
    )
    assert [(u, p["from_user"]) for u, p in env.ws.calls] == [(B, C)]
    assert env.ws.calls[0][1]["signed_sdp"]["sdp"] == "LEG-ANS"


async def test_answers_are_bound_to_participants():
    env = _env()
    env.users.add_user("mallory", "uid-m")
    cid = await _start(env)
    with pytest.raises(PermissionError):
        await env.svc.answer_call(call_id=cid, answerer_user_id="uid-m", sdp_answer="x")
    with pytest.raises(PermissionError):
        await env.svc.answer_call(
            call_id=cid, answerer_user_id=B, sdp_answer="x", to_user_id="uid-m"
        )
    with pytest.raises(PermissionError):
        await env.svc.answer_call(
            call_id=cid, answerer_user_id=B, sdp_answer="x", to_user_id=B
        )


async def test_join_by_an_existing_participant_does_not_rewrite_the_row():
    env = _env()
    cid = await _start(env)
    before = await env.call_repo.get_call(cid)
    await env.svc.answer_call(call_id=cid, answerer_user_id=B, sdp_answer="ANS-B")
    await env.svc.join_call(call_id=cid, joiner_user_id=B, sdp_offers={C: "LEG"})
    after = await env.call_repo.get_call(cid)
    assert after.participant_user_ids == before.participant_user_ids


# ─── ICE ──────────────────────────────────────────────────────────────────


async def test_ice_with_a_target_reaches_that_leg_only():
    env = _env()
    cid = await _start(env)
    env.ws.calls.clear()
    await env.svc.add_ice_candidate(
        call_id=cid, from_user_id=B, candidate={"candidate": "c"}, to_user_id=C
    )
    assert [(u, p["type"], p["from_user"]) for u, p in env.ws.calls] == [
        (C, "call.ice_candidate", B)
    ]
    # Without a target (older client) it still fans out to everyone else.
    env.ws.calls.clear()
    await env.svc.add_ice_candidate(call_id=cid, from_user_id=B, candidate={})
    assert sorted(u for u, _ in env.ws.calls) == [A, C]


async def test_ice_is_bound_to_participants():
    env = _env()
    env.users.add_user("mallory", "uid-m")
    cid = await _start(env)
    with pytest.raises(PermissionError):
        await env.svc.add_ice_candidate(call_id=cid, from_user_id="uid-m", candidate={})
    with pytest.raises(PermissionError):
        await env.svc.add_ice_candidate(
            call_id=cid, from_user_id=B, candidate={}, to_user_id="uid-m"
        )
    assert env.ws.calls and all(p["type"] == "call.ringing" for _, p in env.ws.calls)


# ─── leaving ──────────────────────────────────────────────────────────────


async def test_one_callee_declining_leaves_the_call_ringing_for_the_rest():
    env = _env()
    cid = await _start(env)
    env.ws.calls.clear()
    await env.svc.decline(call_id=cid, decliner_user_id=B)
    assert sorted((u, p["type"], p["by"]) for u, p in env.ws.calls) == [
        (A, "call.declined", B),
        (C, "call.declined", B),
    ]
    assert (await env.call_repo.get_call(cid)).status == "ringing"
    assert env.svc.get_call(cid) is not None
    # The last callee declining ends it as declined.
    await env.svc.decline(call_id=cid, decliner_user_id=C)
    assert (await env.call_repo.get_call(cid)).status == "declined"
    assert env.svc.get_call(cid) is None


async def test_a_group_call_ends_when_fewer_than_two_are_left():
    env = _env()
    cid = await _start(env)
    await env.svc.answer_call(call_id=cid, answerer_user_id=B, sdp_answer="x")
    await env.svc.answer_call(call_id=cid, answerer_user_id=C, sdp_answer="x")
    env.ws.calls.clear()
    await env.svc.hangup(call_id=cid, hanger_user_id=B)
    assert sorted((u, p["by"], p["over"]) for u, p in env.ws.calls) == [
        (A, B, False),
        (C, B, False),
    ]
    assert (await env.call_repo.get_call(cid)).status == "active"
    env.ws.calls.clear()
    await env.svc.hangup(call_id=cid, hanger_user_id=C)
    assert env.ws.calls == [
        (A, {"type": "call.ended", "call_id": cid, "by": C, "over": True})
    ]
    row = await env.call_repo.get_call(cid)
    assert row.status == "ended" and row.duration_seconds is not None
    assert env.svc.get_call(cid) is None


async def test_the_caller_leaving_withdraws_unanswered_invites():
    env = _env()
    cid = await _start(env)
    await env.svc.answer_call(call_id=cid, answerer_user_id=B, sdp_answer="x")
    env.ws.calls.clear()
    await env.svc.hangup(call_id=cid, hanger_user_id=A)
    # carol stops ringing; bob is alone, so the call is over.
    assert sorted((u, p["type"], p["by"]) for u, p in env.ws.calls) == [
        (B, "call.ended", A),
        (C, "call.ended", A),
    ]
    assert (await env.call_repo.get_call(cid)).status == "ended"


async def test_the_callees_stay_connected_after_the_caller_leaves():
    env = _env()
    cid = await _start(env)
    await env.svc.answer_call(call_id=cid, answerer_user_id=B, sdp_answer="x")
    await env.svc.answer_call(call_id=cid, answerer_user_id=C, sdp_answer="x")
    await env.svc.hangup(call_id=cid, hanger_user_id=A)
    assert (await env.call_repo.get_call(cid)).status == "active"
    assert env.svc.get_call(cid).present == {B, C}


async def test_declining_after_answering_is_a_no_op():
    env = _env()
    cid = await _start(env)
    await env.svc.answer_call(call_id=cid, answerer_user_id=B, sdp_answer="x")
    env.ws.calls.clear()
    await env.svc.decline(call_id=cid, decliner_user_id=B)
    assert env.ws.calls == []
    assert B in env.svc.get_call(cid).present


async def test_a_left_participant_gets_no_mesh_offers():
    env = _env()
    cid = await _start(env)
    await env.svc.decline(call_id=cid, decliner_user_id=C)
    await env.svc.answer_call(call_id=cid, answerer_user_id=B, sdp_answer="x")
    env.ws.calls.clear()
    r = await env.svc.join_call(call_id=cid, joiner_user_id=B, sdp_offers={C: "LEG"})
    assert r["joined"] == []
    assert env.ws.calls == []


# ─── cross-household limits ───────────────────────────────────────────────


async def test_a_mesh_answer_to_a_remote_callee_is_not_federated():
    """Group conversations are single-household; an older peer would hand a
    callee-to-callee answer to the caller, so it never goes on the wire."""
    env = _env()
    env.users.add_remote(
        user_id="uid-r", instance_id="remote-inst", remote_username="r"
    )
    env.convos.add_conversation(
        "mixed", ["alice", "bob"], remotes=[("remote-inst", "r", "uid-r")]
    )
    r = await env.svc.initiate_call(
        caller_user_id=A,
        conversation_id="mixed",
        call_type="audio",
        sdp_offers={B: "OB", "uid-r": "OR"},
    )
    env.fed.sent.clear()
    await env.svc.answer_call(
        call_id=r["call_id"], answerer_user_id=B, sdp_answer="x", to_user_id="uid-r"
    )
    assert env.fed.sent == []
    assert r["participants"] == sorted([A, B, "uid-r"])


async def test_an_inbound_answer_is_attributed_to_the_named_callee():
    env = make_call_service()
    env.users.add_user("alice", A)
    for uid, name in (("uid-r1", "r1"), ("uid-r2", "r2")):
        env.users.add_remote(
            user_id=uid, instance_id="remote-inst", remote_username=name
        )
    env.convos.add_conversation(
        "g",
        ["alice"],
        remotes=[("remote-inst", "r1", "uid-r1"), ("remote-inst", "r2", "uid-r2")],
    )
    r = await env.svc.initiate_call(
        caller_user_id=A, conversation_id="g", call_type="audio", sdp_offer="v=0\r\n"
    )
    cid = r["call_id"]
    # Two unanswered callees on that household and no from_user: ambiguous.
    await env.svc.handle_federated_signal(
        _Event(FederationEventType.CALL_ANSWER, "remote-inst", {"call_id": cid})
    )
    assert _frames(env, A, "call.answered") == []
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_ANSWER,
            "remote-inst",
            {"call_id": cid, "from_user": "uid-r2"},
        )
    )
    assert [f["from_user"] for f in _frames(env, A, "call.answered")] == ["uid-r2"]
    # A name the sending household doesn't host is refused.
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_ANSWER,
            "other-inst",
            {"call_id": cid, "from_user": "uid-r1"},
        )
    )
    assert len(_frames(env, A, "call.answered")) == 1
