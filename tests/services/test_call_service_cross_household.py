"""Group calls whose callees sit on different households (v_37).

Seen from a callee's household: the caller rings us with the whole
roster, the other callees' households answer our callee's mesh-leg offer
and trickle ICE to it by name, and one of them hanging up leaves only
them — the call goes on.
"""

from __future__ import annotations

from socialhome.domain.federation import FederationEventType

from ._call_fakes import make_call_service

FET = FederationEventType
A1, B1, C1 = "uid-a1", "uid-b1", "uid-c1"


class _Event:
    def __init__(self, et, from_inst, payload):
        self.event_type = et
        self.from_instance = from_inst
        self.payload = payload


def _env():
    env = make_call_service()
    env.users.add_user("cara", C1)
    env.users.add_remote(user_id=A1, instance_id="inst-a", remote_username="anna")
    env.users.add_remote(user_id=B1, instance_id="inst-b", remote_username="bert")
    env.convos.add_conversation(
        "g", ["cara"], remotes=[("inst-a", "anna", A1), ("inst-b", "bert", B1)]
    )
    return env


def _frames(env, user, type_):
    return [p for u, p in env.ws.calls if u == user and p.get("type") == type_]


async def _ring(env) -> str:
    await env.svc.handle_federated_signal(
        _Event(
            FET.CALL_OFFER,
            "inst-a",
            {
                "call_id": "call-1",
                "conversation_id": "g",
                "from_user": A1,
                "to_user": C1,
                "call_type": "audio",
                "participants": [A1, B1, C1, "uid-outsider"],
            },
        )
    )
    return "call-1"


async def test_the_ring_carries_the_roster_bounded_to_the_conversation():
    env = _env()
    await _ring(env)
    (ring,) = _frames(env, C1, "call.ringing")
    assert ring["participants"] == sorted([A1, B1, C1])
    assert env.svc.get_call("call-1").participants == {A1, B1, C1}


async def test_an_answer_from_another_callees_household_reaches_our_callee():
    env = _env()
    cid = await _ring(env)
    await env.svc.handle_federated_signal(
        _Event(
            FET.CALL_ANSWER,
            "inst-b",
            {"call_id": cid, "from_user": B1, "to_user": C1, "signed_sdp": None},
        )
    )
    (frame,) = _frames(env, C1, "call.answered")
    assert frame["from_user"] == B1
    # A mesh leg is not the caller's answer.
    assert env.svc.get_call(cid).answered == {A1}


async def test_a_mesh_answer_outside_its_seat_reaches_nobody():
    env = _env()
    cid = await _ring(env)
    for sender, payload in (
        ("inst-a", {"from_user": B1, "to_user": C1}),  # names another's user
        ("inst-b", {"from_user": B1, "to_user": A1}),  # target is not ours
        ("inst-b", {"from_user": B1, "to_user": "uid-outsider"}),
    ):
        await env.svc.handle_federated_signal(
            _Event(FET.CALL_ANSWER, sender, {"call_id": cid, **payload})
        )
    assert [u for u, p in env.ws.calls if p.get("type") == "call.answered"] == []


async def test_a_named_ice_candidate_reaches_only_that_participant():
    env = _env()
    cid = await _ring(env)
    await env.svc.handle_federated_signal(
        _Event(
            FET.CALL_ICE_CANDIDATE,
            "inst-b",
            {"call_id": cid, "from_user": B1, "to_user": C1, "candidate": {"c": 1}},
        )
    )
    assert [f["from_user"] for f in _frames(env, C1, "call.ice_candidate")] == [B1]
    await env.svc.handle_federated_signal(
        _Event(
            FET.CALL_ICE_CANDIDATE,
            "inst-b",
            {"call_id": cid, "from_user": B1, "to_user": A1, "candidate": {"c": 2}},
        )
    )
    assert len([p for _u, p in env.ws.calls if p["type"] == "call.ice_candidate"]) == 1


async def test_a_remote_callee_hanging_up_leaves_the_call_going():
    env = _env()
    cid = await _ring(env)
    await env.svc.handle_federated_signal(
        _Event(FET.CALL_HANGUP, "inst-b", {"call_id": cid, "hanger_user": B1})
    )
    (ended,) = _frames(env, C1, "call.ended")
    assert ended["by"] == B1 and ended["over"] is False
    assert env.svc.get_call(cid) is not None
    await env.svc.handle_federated_signal(
        _Event(FET.CALL_HANGUP, "inst-a", {"call_id": cid, "hanger_user": A1})
    )
    assert _frames(env, C1, "call.ended")[-1]["over"] is True
    assert env.svc.get_call(cid) is None
