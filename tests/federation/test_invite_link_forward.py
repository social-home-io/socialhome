"""Transport of a forwarded invite-link mint (v_52): the member household's
waiting request and the host's answer."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from socialhome.domain.federation import (
    DELIVERY_ERROR_QUEUED,
    DeliveryResult,
    FederationEvent,
    FederationEventType,
)
from socialhome.domain.space import HostUnreachableError
from socialhome.federation.invite_link_forward import (
    FORWARDED_INVITE_LINK_ACTION,
    FORWARDED_INVITE_LINK_ACTIONS,
    LIST_INVITE_LINKS_ACTION,
    REVOKE_INVITE_LINK_ACTION,
    InviteLinkForwardCoordinator,
)


class _Registry:
    def __init__(self):
        self.bindings: dict = {}

    def register(self, event_type, handler):
        self.bindings.setdefault(event_type, []).append(handler)


class _Fed:
    """Delivers every send straight into ``peer``'s registry as an event
    from ``me`` — or, with ``deliver=False``, just records it."""

    def __init__(self, me: str, *, result: DeliveryResult | None = None):
        self.me = me
        self._event_registry = _Registry()
        self.peer: _Fed | None = None
        self.sent: list[dict] = []
        self.deliver = True
        self.result = result
        #: Pretend the answer came from this household instead.
        self.answer_as: str | None = None

    async def send_with_mesh_fallback(
        self, *, to_instance_id, event_type, payload, space_id=None
    ):
        self.sent.append(
            {"to": to_instance_id, "event_type": event_type, "payload": payload}
        )
        if self.result is not None:
            return self.result
        if self.deliver and self.peer is not None:
            ev = FederationEvent(
                msg_id="m",
                event_type=event_type,
                from_instance=self.answer_as or self.me,
                to_instance=to_instance_id,
                timestamp="2026-10-04T00:00:00Z",
                payload=payload,
            )
            for handler in self.peer._event_registry.bindings.get(event_type, []):
                await handler(ev)
        return DeliveryResult(instance_id=to_instance_id, ok=True)


class _HostSpaces:
    def __init__(self, answer: dict):
        self.answer = answer
        self.calls: list[dict] = []

    async def handle_forwarded_invite_action(self, space_id, **kwargs):
        self.calls.append({"space_id": space_id, **kwargs})
        return self.answer


def _pair(answer=None, *, timeout=1.0):
    member_fed, host_fed = _Fed("member-1"), _Fed("host-1")
    member_fed.peer, host_fed.peer = host_fed, member_fed
    member = InviteLinkForwardCoordinator(
        federation_service=member_fed, timeout=timeout
    )
    host = InviteLinkForwardCoordinator(federation_service=host_fed)
    spaces = _HostSpaces(answer or {"link": {"token": "tok", "code": "c"}})
    host.attach_space_service(spaces)
    member.attach_to(member_fed)
    host.attach_to(host_fed)
    return SimpleNamespace(
        member=member,
        host=host,
        member_fed=member_fed,
        host_fed=host_fed,
        spaces=spaces,
    )


async def _mint(env, action=FORWARDED_INVITE_LINK_ACTION, **params):
    return await env.member.request(
        action,
        space_id="sp-1",
        host_instance_id="host-1",
        actor_user_id="u-olga",
        # A payload claim the host must NOT honour.
        own_instance_id="someone-else",
        params={"role": "member", **params},
    )


async def test_the_host_answers_with_the_link_it_minted():
    env = _pair()
    answer = await _mint(env, uses=3)
    assert answer["link"] == {"token": "tok", "code": "c"}
    (call,) = env.spaces.calls
    assert call["space_id"] == "sp-1"
    assert call["actor_user_id"] == "u-olga"
    # Bound to the signed sender, never to the payload's actor_instance_id.
    assert call["actor_instance_id"] == "member-1"
    assert call["params"]["uses"] == 3
    assert call["action"] == FORWARDED_INVITE_LINK_ACTION
    (req,) = env.member_fed.sent
    assert req["event_type"] is FederationEventType.SPACE_REMOTE_ADMIN_ACTION
    assert req["payload"]["action"] == FORWARDED_INVITE_LINK_ACTION
    (reply,) = env.host_fed.sent
    assert reply["event_type"] is FederationEventType.SPACE_INVITE_LINK_FORWARD_RESULT
    assert reply["to"] == "member-1"


async def test_a_refusal_comes_back_as_the_error_code():
    env = _pair({"error": "forbidden"})
    answer = await _mint(env)
    assert answer["error"] == "forbidden"
    assert "link" not in answer


async def test_an_offline_host_times_out_as_unreachable():
    env = _pair(timeout=0.05)
    env.member_fed.deliver = False
    with pytest.raises(HostUnreachableError):
        await _mint(env)
    assert env.member._pending == {}


async def test_a_request_that_went_nowhere_is_unreachable_at_once():
    env = _pair()
    env.member_fed.result = DeliveryResult(
        instance_id="host-1", ok=False, error="no_route"
    )
    with pytest.raises(HostUnreachableError):
        await _mint(env)


async def test_a_queued_request_keeps_waiting():
    """Parked in the durable outbox: it will arrive — wait for the answer."""
    env = _pair(timeout=0.05)
    env.member_fed.result = DeliveryResult(
        instance_id="host-1", ok=False, error=DELIVERY_ERROR_QUEUED
    )
    with pytest.raises(HostUnreachableError):  # …and time out, not fail fast
        await _mint(env)


async def test_an_answer_from_another_household_is_ignored():
    env = _pair(timeout=0.05)
    env.host_fed.answer_as = "intruder-9"
    with pytest.raises(HostUnreachableError):
        await _mint(env)


async def test_the_host_ignores_other_admin_actions_and_bad_requests():
    env = _pair()
    base = {
        "msg_id": "m",
        "event_type": FederationEventType.SPACE_REMOTE_ADMIN_ACTION,
        "from_instance": "member-1",
        "to_instance": "host-1",
        "timestamp": "2026-10-04T00:00:00Z",
    }
    for payload in (
        {"space_id": "sp-1", "actor_user_id": "u", "action": "ban", "params": {}},
        # No nonce: nothing to answer.
        {
            "space_id": "sp-1",
            "actor_user_id": "u",
            "action": FORWARDED_INVITE_LINK_ACTION,
            "params": {},
        },
    ):
        await env.host._on_forwarded_request(FederationEvent(payload=payload, **base))
    assert env.spaces.calls == []
    assert env.host_fed.sent == []


async def test_a_late_answer_is_a_no_op():
    env = _pair()
    ev = FederationEvent(
        msg_id="m",
        event_type=FederationEventType.SPACE_INVITE_LINK_FORWARD_RESULT,
        from_instance="host-1",
        to_instance="member-1",
        timestamp="2026-10-04T00:00:00Z",
        payload={"request_nonce": "gone", "link": {}},
    )
    await env.member._on_result(ev)  # no pending request: nothing happens


@pytest.mark.parametrize(
    ("action", "answer"),
    [
        (LIST_INVITE_LINKS_ACTION, {"links": [{"token": "t1"}]}),
        (REVOKE_INVITE_LINK_ACTION, {"revoked": True}),
    ],
)
async def test_list_and_revoke_ride_the_same_round_trip(action, answer):
    env = _pair(answer)
    got = await _mint(env, action=action, token="t1")
    assert {k: got[k] for k in answer} == answer
    (call,) = env.spaces.calls
    assert call["action"] == action
    assert call["actor_instance_id"] == "member-1"
    assert call["params"]["token"] == "t1"


async def test_an_unknown_action_is_never_sent_or_answered():
    env = _pair()
    with pytest.raises(ValueError):
        await _mint(env, action="dissolve")
    assert env.member_fed.sent == []
    assert {
        FORWARDED_INVITE_LINK_ACTION,
        LIST_INVITE_LINKS_ACTION,
        REVOKE_INVITE_LINK_ACTION,
    } == FORWARDED_INVITE_LINK_ACTIONS
