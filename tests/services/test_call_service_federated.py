"""Federated-path coverage for CallSignalingService (spec §26)."""

from __future__ import annotations

from social_home.crypto import generate_identity_keypair
from social_home.domain.federation import FederationEventType
from social_home.federation.sdp_signing import (
    sign_rtc_offer,
    signed_sdp_to_dict,
)
from social_home.services.call_service import RINGING_TTL_SECONDS

from ._call_fakes import FakeFederation, make_call_service


class _Event:
    def __init__(self, et, from_inst, payload):
        self.event_type = et
        self.from_instance = from_inst
        self.payload = payload


def _federated_env():
    """Set up bob locally + alice remote on ``conv-ab``."""
    env = make_call_service()
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
    """CALL_QUALITY persists the sample even without a local record."""
    env = make_call_service()
    await env.svc.handle_federated_signal(
        _Event(
            FederationEventType.CALL_QUALITY,
            "remote-inst",
            {
                "call_id": "stale",
                "rtt_ms": 42,
                "reporter_user": "uid-remote",
                "sampled_at": 1700000000,
            },
        )
    )
    samples = await env.call_repo.list_quality_samples("stale")
    assert samples and samples[0].rtt_ms == 42


# ─── Signed SDP verification path ─────────────────────────────────────────


async def test_handle_call_offer_with_signed_sdp_passes_verification():
    """When sender's pk is known, SDP signature is verified before forwarding."""
    sender_kp = generate_identity_keypair()
    env = make_call_service(
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
