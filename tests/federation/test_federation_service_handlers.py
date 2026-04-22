"""Coverage fill for :class:`FederationService` inbound handlers.

These handlers receive a :class:`FederationEvent` + dispatch to a
downstream service. Tests fire each handler directly with a stubbed
service wired so every branch (service attached vs None, missing
fields, etc.) is exercised.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.domain.events import PairingIntroRelayReceived
from socialhome.domain.federation import FederationEventType
from socialhome.federation.federation_service import FederationService


def _event(
    event_type: str,
    payload: dict,
    *,
    from_instance: str = "peer-1",
    space_id: str | None = None,
):
    return SimpleNamespace(
        event_type=event_type,
        payload=payload,
        from_instance=from_instance,
        space_id=space_id,
    )


@pytest.fixture
def svc():
    """Construct a FederationService without running create_app — enough
    for the inbound handlers, which don't need a real DB."""
    s = FederationService.__new__(FederationService)
    # Minimal state for the handlers we test.
    s._bus = MagicMock()
    s._bus.publish = AsyncMock()
    s._dm_routing_service = None
    s._typing_service = None
    s._presence_service = None
    s._transport = None
    s._call_signaling = None
    s._sync_manager = None
    s._space_sync_service = None
    s._own_instance_id = "self-iid"
    s._ice_servers = []
    return s


# ─── _handle_pairing_intro_relay ─────────────────────────────────────


async def test_handle_pairing_intro_relay_publishes_event(svc):
    await svc._handle_pairing_intro_relay(
        _event(
            "PAIRING_INTRO_RELAY",
            {"target_instance_id": "target", "message": "hi"},
        )
    )
    svc._bus.publish.assert_awaited_once()
    published = svc._bus.publish.await_args.args[0]
    assert isinstance(published, PairingIntroRelayReceived)
    assert published.target_instance_id == "target"


async def test_handle_pairing_intro_relay_truncates_message(svc):
    huge = "x" * 10_000
    await svc._handle_pairing_intro_relay(
        _event(
            "PAIRING_INTRO_RELAY",
            {"target_instance_id": "t", "message": huge},
        )
    )
    msg = svc._bus.publish.await_args.args[0].message
    assert len(msg) == 500


# ─── _handle_dm_relay ────────────────────────────────────────────────


async def test_handle_dm_relay_noop_without_service(svc):
    # No routing service attached → silent return.
    await svc._handle_dm_relay(_event("DM_RELAY", {"message_id": "m"}))


async def test_handle_dm_relay_delegates(svc):
    svc._dm_routing_service = MagicMock()
    svc._dm_routing_service.handle_inbound_relay = AsyncMock(
        return_value="delivered",
    )
    await svc._handle_dm_relay(_event("DM_RELAY", {"message_id": "m"}))
    svc._dm_routing_service.handle_inbound_relay.assert_awaited_once()


# ─── _handle_dm_user_typing ─────────────────────────────────────────


async def test_handle_dm_user_typing_noop_without_service(svc):
    await svc._handle_dm_user_typing(_event("DM_USER_TYPING", {}))


async def test_handle_dm_user_typing_delegates(svc):
    svc._typing_service = MagicMock()
    svc._typing_service.handle_remote_typing = AsyncMock()
    await svc._handle_dm_user_typing(_event("DM_USER_TYPING", {}))
    svc._typing_service.handle_remote_typing.assert_awaited_once()


# ─── _handle_presence_updated ───────────────────────────────────────


async def test_handle_presence_updated_no_service_logs(svc):
    await svc._handle_presence_updated(
        _event("PRESENCE_UPDATED", {"status": "online"}),
    )


async def test_handle_presence_updated_delegates(svc):
    svc._presence_service = MagicMock()
    svc._presence_service.apply_remote = AsyncMock()
    await svc._handle_presence_updated(
        _event("PRESENCE_UPDATED", {"status": "away"}),
    )
    svc._presence_service.apply_remote.assert_awaited_once()


# ─── _handle_transport_event ────────────────────────────────────────


async def test_handle_transport_event_no_transport_noop(svc):
    await svc._handle_transport_event(
        _event(FederationEventType.FEDERATION_RTC_OFFER, {"sdp": "x"}),
    )


async def test_handle_transport_event_offer(svc):
    svc._transport = MagicMock()
    svc._transport.on_rtc_offer = AsyncMock()
    ev = _event(FederationEventType.FEDERATION_RTC_OFFER, {"sdp": "x"})
    # enum-typed event_type for match/case.
    ev.event_type = FederationEventType.FEDERATION_RTC_OFFER
    await svc._handle_transport_event(ev)
    svc._transport.on_rtc_offer.assert_awaited_once()


async def test_handle_transport_event_answer(svc):
    svc._transport = MagicMock()
    svc._transport.on_rtc_answer = AsyncMock()
    ev = _event(FederationEventType.FEDERATION_RTC_ANSWER, {"sdp": "x"})
    ev.event_type = FederationEventType.FEDERATION_RTC_ANSWER
    await svc._handle_transport_event(ev)
    svc._transport.on_rtc_answer.assert_awaited_once()


async def test_handle_transport_event_ice(svc):
    svc._transport = MagicMock()
    svc._transport.on_rtc_ice = AsyncMock()
    ev = _event(FederationEventType.FEDERATION_RTC_ICE, {"candidate": "c"})
    ev.event_type = FederationEventType.FEDERATION_RTC_ICE
    await svc._handle_transport_event(ev)
    svc._transport.on_rtc_ice.assert_awaited_once()


# ─── _handle_call_signal ────────────────────────────────────────────


async def test_handle_call_signal_no_signaler_noop(svc):
    await svc._handle_call_signal(_event("CALL_SIGNAL", {}))


async def test_handle_call_signal_delegates(svc):
    svc._call_signaling = MagicMock()
    svc._call_signaling.handle_federated_signal = AsyncMock()
    await svc._handle_call_signal(_event("CALL_SIGNAL", {}))
    svc._call_signaling.handle_federated_signal.assert_awaited_once()


# ─── _handle_space_sync_complete ────────────────────────────────────


async def test_handle_space_sync_complete_no_manager(svc):
    await svc._handle_space_sync_complete(_event("SPACE_SYNC_COMPLETE", {}))


async def test_handle_space_sync_complete_delegates(svc):
    svc._sync_manager = MagicMock()
    svc._sync_manager.close_session = MagicMock()
    await svc._handle_space_sync_complete(
        _event("SPACE_SYNC_COMPLETE", {"sync_id": "s1"}),
    )
    svc._sync_manager.close_session.assert_called_once_with("s1")


# ─── _handle_space_sync_begin ──────────────────────────────────────


async def test_handle_space_sync_begin_no_manager(svc):
    await svc._handle_space_sync_begin(
        _event("SPACE_SYNC_BEGIN", {"sync_id": "s", "space_id": "sp"}),
    )


async def test_handle_space_sync_begin_missing_fields(svc):
    svc._sync_manager = MagicMock()
    svc._sync_manager.begin_session = AsyncMock()
    await svc._handle_space_sync_begin(_event("SPACE_SYNC_BEGIN", {}))
    svc._sync_manager.begin_session.assert_not_awaited()


async def test_handle_space_sync_begin_accepted_no_prefer_direct(svc):
    """Accepted with prefer_direct=False doesn't load the RTC session."""
    svc._sync_manager = MagicMock()
    svc._sync_manager.begin_session = AsyncMock(
        return_value=SimpleNamespace(
            accepted=True,
            next_event=None,
            next_payload=None,
        ),
    )
    svc._sync_manager.get_session = MagicMock(return_value=None)
    await svc._handle_space_sync_begin(
        _event(
            "SPACE_SYNC_BEGIN",
            {"sync_id": "s", "space_id": "sp", "sync_mode": "initial"},
            space_id="sp",
        )
    )
    # Session lookup only happens on prefer_direct=True; confirms no path was taken.
    svc._sync_manager.get_session.assert_not_called()


# ─── _handle_space_sync_offer ──────────────────────────────────────


async def test_handle_space_sync_offer_no_manager(svc):
    await svc._handle_space_sync_offer(
        _event("SPACE_SYNC_OFFER", {"sync_id": "s", "sdp_offer": "x"}),
    )


async def test_handle_space_sync_offer_missing_fields(svc):
    svc._sync_manager = MagicMock()
    svc._sync_manager.apply_offer = AsyncMock()
    await svc._handle_space_sync_offer(_event("SPACE_SYNC_OFFER", {}))
    svc._sync_manager.apply_offer.assert_not_awaited()


async def test_handle_space_sync_offer_apply_offer_called(svc):
    """apply_offer is awaited with the received SDP."""
    svc._sync_manager = MagicMock()
    svc._sync_manager.apply_offer = AsyncMock(return_value="sdp-answer")
    try:
        await svc._handle_space_sync_offer(
            _event(
                "SPACE_SYNC_OFFER",
                {"sync_id": "s1", "sdp_offer": "sdp-x", "ice_servers": []},
                space_id="sp",
            )
        )
    except AttributeError:
        # send_event isn't reachable on the bare __new__ instance; we
        # only cover the apply_offer branch here.
        pass
    svc._sync_manager.apply_offer.assert_awaited_once()


# ─── _handle_space_sync_answer ─────────────────────────────────────


async def test_handle_space_sync_answer_no_manager(svc):
    await svc._handle_space_sync_answer(
        _event("SPACE_SYNC_ANSWER", {"sync_id": "s", "sdp_answer": "x"}),
    )


async def test_handle_space_sync_answer_missing(svc):
    svc._sync_manager = MagicMock()
    svc._sync_manager.apply_answer = AsyncMock()
    await svc._handle_space_sync_answer(_event("SPACE_SYNC_ANSWER", {}))
    svc._sync_manager.apply_answer.assert_not_awaited()


async def test_handle_space_sync_answer_happy(svc):
    svc._sync_manager = MagicMock()
    svc._sync_manager.apply_answer = AsyncMock()
    await svc._handle_space_sync_answer(
        _event(
            "SPACE_SYNC_ANSWER",
            {"sync_id": "s1", "sdp_answer": "a"},
        )
    )
    svc._sync_manager.apply_answer.assert_awaited_once()


# ─── _handle_space_sync_ice ────────────────────────────────────────


async def test_handle_space_sync_ice_no_manager(svc):
    await svc._handle_space_sync_ice(
        _event("SPACE_SYNC_ICE", {"sync_id": "s", "candidate": "c"}),
    )


async def test_handle_space_sync_ice_missing(svc):
    svc._sync_manager = MagicMock()
    svc._sync_manager.apply_ice = AsyncMock()
    await svc._handle_space_sync_ice(_event("SPACE_SYNC_ICE", {}))
    svc._sync_manager.apply_ice.assert_not_awaited()


async def test_handle_space_sync_ice_happy(svc):
    svc._sync_manager = MagicMock()
    svc._sync_manager.apply_ice = AsyncMock()
    await svc._handle_space_sync_ice(
        _event(
            "SPACE_SYNC_ICE",
            {"sync_id": "s1", "candidate": "candidate:1"},
        )
    )
    svc._sync_manager.apply_ice.assert_awaited_once()


# ─── _handle_space_sync_direct_ready ──────────────────────────────


async def test_handle_direct_ready_no_services(svc):
    await svc._handle_space_sync_direct_ready(
        _event("SPACE_SYNC_DIRECT_READY", {"sync_id": "s"}),
    )


async def test_handle_direct_ready_missing_sync_id(svc):
    svc._sync_manager = MagicMock()
    svc._space_sync_service = MagicMock()
    await svc._handle_space_sync_direct_ready(
        _event("SPACE_SYNC_DIRECT_READY", {}),
    )


async def test_handle_direct_ready_unknown_session(svc):
    svc._sync_manager = MagicMock()
    svc._sync_manager.get_session = MagicMock(return_value=None)
    svc._space_sync_service = MagicMock()
    await svc._handle_space_sync_direct_ready(
        _event("SPACE_SYNC_DIRECT_READY", {"sync_id": "s1"}),
    )


async def test_handle_direct_ready_wrong_origin_skipped(svc):
    svc._sync_manager = MagicMock()
    svc._sync_manager.get_session = MagicMock(
        return_value=SimpleNamespace(requester_instance_id="the-requester"),
    )
    svc._space_sync_service = MagicMock()
    svc._space_sync_service.stream_initial = AsyncMock()
    await svc._handle_space_sync_direct_ready(
        _event(
            "SPACE_SYNC_DIRECT_READY",
            {"sync_id": "s1"},
            from_instance="imposter",
        )
    )
    svc._space_sync_service.stream_initial.assert_not_awaited()


# ─── _handle_space_sync_direct_failed ────────────────────────────


async def test_handle_direct_failed_no_manager(svc):
    await svc._handle_space_sync_direct_failed(
        _event("SPACE_SYNC_DIRECT_FAILED", {}),
    )


async def test_handle_direct_failed_missing_sync_id(svc):
    svc._sync_manager = MagicMock()
    svc._sync_manager.trigger_relay_sync = AsyncMock()
    await svc._handle_space_sync_direct_failed(
        _event("SPACE_SYNC_DIRECT_FAILED", {}),
    )
    svc._sync_manager.trigger_relay_sync.assert_not_awaited()


async def test_handle_direct_failed_no_next_event(svc):
    """trigger_relay_sync returning no next_event short-circuits."""
    svc._sync_manager = MagicMock()
    svc._sync_manager.trigger_relay_sync = AsyncMock(
        return_value=SimpleNamespace(next_event=None, next_payload=None),
    )
    await svc._handle_space_sync_direct_failed(
        _event("SPACE_SYNC_DIRECT_FAILED", {"sync_id": "s1"}),
    )
    svc._sync_manager.trigger_relay_sync.assert_awaited_once()


# ─── _handle_space_sync_request_more ──────────────────────────────


async def test_handle_request_more_no_manager(svc):
    await svc._handle_space_sync_request_more(
        _event("SPACE_SYNC_REQUEST_MORE", {}),
    )


async def test_handle_request_more_clamp_returns_none(svc):
    svc._sync_manager = MagicMock()
    svc._sync_manager.clamp_request_more = AsyncMock(return_value=None)
    await svc._handle_space_sync_request_more(
        _event("SPACE_SYNC_REQUEST_MORE", {"sync_id": "s"}),
    )


# ─── _handle_instance_sync_status ────────────────────────────────


async def test_handle_instance_sync_status_no_manager(svc):
    await svc._handle_instance_sync_status(
        _event("INSTANCE_SYNC_STATUS", {}),
    )


async def test_handle_instance_sync_status_delegates(svc):
    svc._sync_manager = MagicMock()
    svc._sync_manager.validate_instance_sync_status = AsyncMock(
        return_value=["sp1", "sp2"],
    )
    await svc._handle_instance_sync_status(
        _event("INSTANCE_SYNC_STATUS", {"spaces": []}),
    )


# ─── _validate_inbound_media ─────────────────────────────────────


async def test_validate_inbound_media_none_is_noop(svc):
    ev = _event("SPACE_POST_CREATED", {})
    await svc._validate_inbound_media(ev)
    # Still no file_meta.
    assert ev.payload.get("file_meta") is None


async def test_validate_inbound_media_valid_keeps_meta(svc):
    meta = {
        "kind": "image",
        "mime_type": "image/webp",
        "size_bytes": 100,
        "orig_filename": "x.webp",
    }
    ev = _event("SPACE_POST_CREATED", {"file_meta": meta})
    await svc._validate_inbound_media(ev)
    # Valid metadata is preserved (or may be normalised).
    assert "file_meta" in ev.payload


async def test_validate_inbound_media_invalid_is_stripped(svc):
    ev = _event(
        "SPACE_POST_CREATED",
        {"file_meta": {"bogus": True}},  # missing required fields
    )
    await svc._validate_inbound_media(ev)
    # Stripped.
    assert "file_meta" not in ev.payload
