"""Additional coverage for CallSignalingService edge cases."""

from __future__ import annotations

from socialhome.domain.federation import FederationEventType

from ._call_fakes import make_call_service


class _Event:
    def __init__(self, et, from_inst, payload):
        self.event_type = et
        self.from_instance = from_inst
        self.payload = payload


# ─── attach_ws_manager / attach_federation / attach_push_service ─────────


def test_attach_ws_manager_replaces():
    env = make_call_service()
    env.svc.attach_ws_manager(object())
    assert env.svc._ws_manager is not None


def test_attach_federation_replaces():
    env = make_call_service()
    env.svc.attach_federation(object())
    assert env.svc._federation is not None


def test_attach_push_service_replaces():
    env = make_call_service()
    env.svc.attach_push_service(object())
    assert env.svc._push is not None


# ─── Empty user_id branch in _fanout_to_user ─────────────────────────────


async def test_fanout_with_empty_user_is_noop():
    env = make_call_service()
    # Neither raises nor errors.
    await env.svc._fanout_to_user("", {"x": 1})
    await env.svc._fanout_to_user("alice", {"x": 1})


# ─── Federated CALL_END / unknown-call edge cases ────────────────────────


async def test_handle_call_end_cleans_record():
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
            FederationEventType.CALL_END,
            "remote-inst",
            {"call_id": "c1", "hanger_user": "uid-alice"},
        )
    )
    assert env.svc.get_call("c1") is None


async def test_handle_ice_for_unknown_call_is_silent():
    env = make_call_service()
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_ICE_CANDIDATE,
            "remote-inst",
            {"call_id": "missing", "from_user": "uid-alice", "candidate": {}},
        )
    )


async def test_handle_call_answer_for_unknown_call_is_silent():
    env = make_call_service()
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_ANSWER,
            "remote-inst",
            {"call_id": "missing", "signed_sdp": None},
        )
    )


# ─── list_calls_for_user when none ───────────────────────────────────────


def test_list_calls_for_unknown_user_returns_empty():
    env = make_call_service()
    assert env.svc.list_calls_for_user("nobody") == []
