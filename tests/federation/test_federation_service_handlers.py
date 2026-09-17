"""Coverage fill for :class:`FederationService` inbound handlers.

These handlers receive a :class:`FederationEvent` + dispatch to a
downstream service. Tests fire each handler directly with a stubbed
service wired so every branch (service attached vs None, missing
fields, etc.) is exercised.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from socialhome.domain.events import PairingIntroRelayReceived
from socialhome.domain.federation import FederationEventType, PairingStatus
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
    s._space_sync_receiver = None
    s._gfs_connection_service = None
    # #648 — the mesh BEGIN path drops the cached route to the requester
    # before streaming, and rejections go out over the mesh fallback.
    s._route_service = None
    s._routed_handler = None
    s._last_mesh_begin_at = {}
    s._own_instance_id = "self-iid"
    s._own_identity_seed = b"\x00" * 32
    s._ice_servers = []
    # Paired by default: ``is_confirmed_peer`` / ``send_with_mesh_fallback``
    # read ``remote_instances`` through this repo, so the requester-side
    # sync helpers short-circuit into (the usually patched) ``send_event``
    # exactly as they did before the mesh-aware conversion. Mesh tests
    # flip ``get_instance`` to return ``None`` (no pairing row).
    s._federation_repo = MagicMock()
    s._federation_repo.get_instance = AsyncMock(
        return_value=SimpleNamespace(status=PairingStatus.CONFIRMED),
    )
    return s


def _unpair(svc) -> None:
    """Make every counterpart look like a mesh-only household — no
    ``remote_instances`` row at all (the shape that used to hit
    ``send_event: unknown instance``)."""
    svc._federation_repo.get_instance = AsyncMock(return_value=None)


def _attach_mesh(svc, *, target: str):
    """Wire a route service + routed handler so ``send_with_mesh_fallback``
    takes the SPACE_ROUTED branch. Returns the ``send_routed`` mock."""
    route_service = MagicMock()
    route_service.cooldown_remaining = MagicMock(return_value=0.0)
    route_service.discover_route = AsyncMock(
        return_value=([svc._own_instance_id, "relay", target], "eph-pk"),
    )
    route_service.invalidate = AsyncMock()
    routed_handler = MagicMock()
    routed_handler.send_routed = AsyncMock(return_value="route-id")
    svc._route_service = route_service
    svc._routed_handler = routed_handler
    return routed_handler.send_routed


def _unknown_instance_warnings(caplog) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "unknown instance" in r.getMessage()
    ]


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
    """Accepted with ``prefer_direct=False`` flips the session into
    HTTPS-mode (Part C) and immediately schedules ``stream_initial``
    — the chunks ride ``SPACE_SYNC_CHUNK`` federation events instead
    of waiting on an SDP / ICE handshake. Without this path the
    relay-fallback ``trigger_relay_sync`` returned a BEGIN that the
    provider accepted and then ignored, so a peer that couldn't
    open a DataChannel got stuck."""
    record = SimpleNamespace(
        sync_id="s",
        rtc=None,
        transport_mode="rtc",
    )
    svc._sync_manager = MagicMock()
    svc._sync_manager.begin_session = AsyncMock(
        return_value=SimpleNamespace(
            accepted=True,
            next_event=None,
            next_payload=None,
        ),
    )
    svc._sync_manager.get_session = MagicMock(return_value=record)
    svc._space_sync_service = MagicMock()
    svc._space_sync_service.stream_initial = AsyncMock()
    await svc._handle_space_sync_begin(
        _event(
            "SPACE_SYNC_BEGIN",
            {"sync_id": "s", "space_id": "sp", "sync_mode": "initial"},
            space_id="sp",
        )
    )
    # Yield once so the create_task body runs.
    await asyncio.sleep(0)
    assert record.transport_mode == "https"
    svc._space_sync_service.stream_initial.assert_awaited_once_with(record)


async def test_handle_space_sync_begin_offer_includes_signaling_node(svc):
    """Provider asks GFS for signaling_node and embeds it in OFFER (§24.10.7)."""
    record = SimpleNamespace(signaling_node=None)
    record.rtc = SimpleNamespace(create_offer=AsyncMock(return_value="sdp-x"))
    svc._sync_manager = MagicMock()
    svc._sync_manager.begin_session = AsyncMock(
        return_value=SimpleNamespace(
            accepted=True,
            next_event=None,
            next_payload=None,
        ),
    )
    svc._sync_manager.get_session = MagicMock(return_value=record)
    svc._gfs_connection_service = MagicMock()
    svc._gfs_connection_service.request_signaling_node = AsyncMock(
        return_value="https://b.gfs.test",
    )
    with (
        patch.object(
            FederationService,
            "is_confirmed_peer",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch.object(
            FederationService,
            "send_event",
            new_callable=AsyncMock,
        ) as send_mock,
    ):
        await svc._handle_space_sync_begin(
            _event(
                "SPACE_SYNC_BEGIN",
                {
                    "sync_id": "s1",
                    "space_id": "sp",
                    "sync_mode": "initial",
                    "prefer_direct": True,
                },
                space_id="sp",
            ),
        )
        sent_payload = send_mock.await_args.kwargs["payload"]
    assert sent_payload["signaling_node"] == "https://b.gfs.test"
    # Record stores it so DIRECT_READY/FAILED can release the slot.
    assert record.signaling_node == "https://b.gfs.test"


async def test_handle_space_sync_begin_offer_omits_signaling_node_when_null(svc):
    """Single-node GFS returns null → field omitted from OFFER."""
    record = SimpleNamespace(signaling_node=None)
    record.rtc = SimpleNamespace(create_offer=AsyncMock(return_value="sdp-x"))
    svc._sync_manager = MagicMock()
    svc._sync_manager.begin_session = AsyncMock(
        return_value=SimpleNamespace(
            accepted=True,
            next_event=None,
            next_payload=None,
        ),
    )
    svc._sync_manager.get_session = MagicMock(return_value=record)
    svc._gfs_connection_service = MagicMock()
    svc._gfs_connection_service.request_signaling_node = AsyncMock(return_value=None)
    with (
        patch.object(
            FederationService,
            "is_confirmed_peer",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch.object(
            FederationService,
            "send_event",
            new_callable=AsyncMock,
        ) as send_mock,
    ):
        await svc._handle_space_sync_begin(
            _event(
                "SPACE_SYNC_BEGIN",
                {
                    "sync_id": "s2",
                    "space_id": "sp",
                    "sync_mode": "initial",
                    "prefer_direct": True,
                },
                space_id="sp",
            ),
        )
        sent_payload = send_mock.await_args.kwargs["payload"]
    assert "signaling_node" not in sent_payload
    assert record.signaling_node is None


async def test_handle_space_sync_begin_no_gfs_service_no_signaling_node(svc):
    """No GFS attached (HFS-only) → OFFER stays bare, no crash."""
    record = SimpleNamespace(signaling_node=None)
    record.rtc = SimpleNamespace(create_offer=AsyncMock(return_value="sdp-x"))
    svc._sync_manager = MagicMock()
    svc._sync_manager.begin_session = AsyncMock(
        return_value=SimpleNamespace(
            accepted=True,
            next_event=None,
            next_payload=None,
        ),
    )
    svc._sync_manager.get_session = MagicMock(return_value=record)
    # _gfs_connection_service stays None.
    with (
        patch.object(
            FederationService,
            "is_confirmed_peer",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch.object(
            FederationService,
            "send_event",
            new_callable=AsyncMock,
        ) as send_mock,
    ):
        await svc._handle_space_sync_begin(
            _event(
                "SPACE_SYNC_BEGIN",
                {
                    "sync_id": "s3",
                    "space_id": "sp",
                    "sync_mode": "initial",
                    "prefer_direct": True,
                },
                space_id="sp",
            ),
        )
        sent_payload = send_mock.await_args.kwargs["payload"]
    assert "signaling_node" not in sent_payload


async def test_handle_space_sync_begin_rejected_sends_to_v20_peer(svc):
    """A non-member SPACE_SYNC_BEGIN from a v_20+ peer triggers a
    SPACE_SYNC_REJECTED reply so the member can reconcile its stub.

    Routed through ``send_with_mesh_fallback`` rather than ``send_event``
    (#648): a mesh-only requester is by definition not a CONFIRMED peer,
    so a bare ``send_event`` reply is silence — and a requester met with
    silence retries until it hits the rate limit.
    """
    svc._sync_manager = MagicMock()
    svc._sync_manager.begin_session = AsyncMock(
        return_value=SimpleNamespace(
            accepted=False,
            reason="not_a_member",
            next_event=FederationEventType.SPACE_SYNC_REJECTED,
            next_payload={
                "sync_id": "s9",
                "space_id": "sp",
                "reason": "removed",
            },
        ),
    )
    with (
        patch.object(
            FederationService,
            "peer_supports",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch.object(
            FederationService,
            "send_with_mesh_fallback",
            new_callable=AsyncMock,
        ) as send_mock,
    ):
        await svc._handle_space_sync_begin(
            _event(
                "SPACE_SYNC_BEGIN",
                {"sync_id": "s9", "space_id": "sp"},
                space_id="sp",
            ),
        )
    send_mock.assert_awaited_once()
    kwargs = send_mock.await_args.kwargs
    assert kwargs["event_type"] is FederationEventType.SPACE_SYNC_REJECTED
    assert kwargs["payload"]["reason"] == "removed"


async def test_handle_space_sync_begin_rejected_silent_for_sub_v20_peer(svc):
    """The SAME reject against a sub-v_20 peer (no SPACE_SYNC_REJECTED
    handler) falls back to the S-1 silent drop — no send."""
    svc._sync_manager = MagicMock()
    svc._sync_manager.begin_session = AsyncMock(
        return_value=SimpleNamespace(
            accepted=False,
            reason="not_a_member",
            next_event=FederationEventType.SPACE_SYNC_REJECTED,
            next_payload={
                "sync_id": "s10",
                "space_id": "sp",
                "reason": "dissolved",
            },
        ),
    )
    with (
        patch.object(
            FederationService,
            "peer_supports",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch.object(
            FederationService,
            "send_event",
            new_callable=AsyncMock,
        ) as send_mock,
    ):
        await svc._handle_space_sync_begin(
            _event(
                "SPACE_SYNC_BEGIN",
                {"sync_id": "s10", "space_id": "sp"},
                space_id="sp",
            ),
        )
    send_mock.assert_not_awaited()


async def test_handle_space_sync_begin_mesh_requester_forced_https(svc):
    """A mesh-only (non-confirmed) requester is forced to HTTPS mode
    even when its BEGIN says ``prefer_direct=True`` — WebRTC ICE can't
    traverse a relay, so the host streams chunks over SPACE_SYNC_CHUNK
    instead of an RTC offer it could never deliver."""
    record = SimpleNamespace(
        sync_id="m1",
        rtc=None,
        transport_mode="rtc",
    )
    svc._sync_manager = MagicMock()
    svc._sync_manager.begin_session = AsyncMock(
        return_value=SimpleNamespace(
            accepted=True,
            next_event=None,
            next_payload=None,
        ),
    )
    svc._sync_manager.get_session = MagicMock(return_value=record)
    svc._space_sync_service = MagicMock()
    svc._space_sync_service.stream_initial = AsyncMock()
    with (
        patch.object(
            FederationService,
            "is_confirmed_peer",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch.object(
            FederationService,
            "send_event",
            new_callable=AsyncMock,
        ) as send_mock,
    ):
        await svc._handle_space_sync_begin(
            _event(
                "SPACE_SYNC_BEGIN",
                {
                    "sync_id": "m1",
                    "space_id": "sp",
                    "sync_mode": "initial",
                    "prefer_direct": True,
                },
                space_id="sp",
            ),
        )
        await asyncio.sleep(0)
    assert record.transport_mode == "https"
    svc._space_sync_service.stream_initial.assert_awaited_once_with(record)
    # No RTC offer is sent to a peer that can't complete the handshake.
    send_mock.assert_not_awaited()


async def test_handle_space_sync_begin_confirmed_prefer_direct_uses_rtc(svc):
    """A CONFIRMED direct peer with ``prefer_direct=True`` still takes
    the RTC path — an SDP offer is built and SPACE_SYNC_OFFER is sent."""
    record = SimpleNamespace(signaling_node=None)
    record.rtc = SimpleNamespace(create_offer=AsyncMock(return_value="sdp-x"))
    svc._sync_manager = MagicMock()
    svc._sync_manager.begin_session = AsyncMock(
        return_value=SimpleNamespace(
            accepted=True,
            next_event=None,
            next_payload=None,
        ),
    )
    svc._sync_manager.get_session = MagicMock(return_value=record)
    with (
        patch.object(
            FederationService,
            "is_confirmed_peer",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch.object(
            FederationService,
            "send_event",
            new_callable=AsyncMock,
        ) as send_mock,
    ):
        await svc._handle_space_sync_begin(
            _event(
                "SPACE_SYNC_BEGIN",
                {
                    "sync_id": "c1",
                    "space_id": "sp",
                    "sync_mode": "initial",
                    "prefer_direct": True,
                },
                space_id="sp",
            ),
        )
    record.rtc.create_offer.assert_awaited_once()
    send_mock.assert_awaited_once()
    assert (
        send_mock.await_args.kwargs["event_type"]
        is FederationEventType.SPACE_SYNC_OFFER
    )


async def test_handle_space_sync_begin_confirmed_no_prefer_direct_uses_https(svc):
    """A CONFIRMED direct peer with ``prefer_direct=False`` still uses
    HTTPS mode (unchanged behaviour — the requester asked for relay)."""
    record = SimpleNamespace(
        sync_id="c2",
        rtc=None,
        transport_mode="rtc",
    )
    svc._sync_manager = MagicMock()
    svc._sync_manager.begin_session = AsyncMock(
        return_value=SimpleNamespace(
            accepted=True,
            next_event=None,
            next_payload=None,
        ),
    )
    svc._sync_manager.get_session = MagicMock(return_value=record)
    svc._space_sync_service = MagicMock()
    svc._space_sync_service.stream_initial = AsyncMock()
    with (
        patch.object(
            FederationService,
            "is_confirmed_peer",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch.object(
            FederationService,
            "send_event",
            new_callable=AsyncMock,
        ) as send_mock,
    ):
        await svc._handle_space_sync_begin(
            _event(
                "SPACE_SYNC_BEGIN",
                {"sync_id": "c2", "space_id": "sp", "sync_mode": "initial"},
                space_id="sp",
            ),
        )
        await asyncio.sleep(0)
    assert record.transport_mode == "https"
    svc._space_sync_service.stream_initial.assert_awaited_once_with(record)
    send_mock.assert_not_awaited()


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


# ─── Part A: requester-side direct-ready / direct-failed watcher ──


async def test_offer_handler_spawns_ready_watcher(svc):
    """After ``apply_offer`` builds the answer and we ship it back,
    the requester MUST start a watcher that emits DIRECT_READY when
    the DataChannel opens. Before this fix nothing did, so the
    provider sat waiting forever and the sync silently failed."""
    rtc_session = SimpleNamespace(
        wait_ready=AsyncMock(return_value=True),
        recv_chunk=AsyncMock(side_effect=ConnectionError("eof")),
    )
    record = SimpleNamespace(
        sync_id="s",
        space_id="sp",
        rtc=rtc_session,
        rtc_watcher=None,
    )
    svc._sync_manager = MagicMock()
    svc._sync_manager.apply_offer = AsyncMock(return_value="sdp-ans")
    svc._sync_manager.get_session = MagicMock(return_value=record)
    svc._space_sync_receiver = SimpleNamespace(on_chunk=AsyncMock())
    with patch.object(
        FederationService,
        "send_event",
        new_callable=AsyncMock,
    ) as send_mock:
        await svc._handle_space_sync_offer(
            _event(
                "SPACE_SYNC_OFFER",
                {"sync_id": "s", "sdp_offer": "x"},
                space_id="sp",
            ),
        )
        # Drive the watcher one tick so the wait_ready + emit run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # The answer ships immediately; the ready event fires after
        # the watcher resolves.
        sent_events = [c.kwargs["event_type"].value for c in send_mock.await_args_list]
    assert "space_sync_answer" in sent_events
    assert "space_sync_direct_ready" in sent_events


async def test_offer_handler_emits_direct_failed_on_ice_timeout(svc):
    """When ``wait_ready`` returns ``False`` (the 15 s ICE timeout
    expired), the watcher MUST send DIRECT_FAILED back to the
    provider so the existing relay-fallback hook re-issues the BEGIN
    with ``prefer_direct=False``. Before this, the requester just
    sat there with the session half-open."""
    rtc_session = SimpleNamespace(
        wait_ready=AsyncMock(return_value=False),
        recv_chunk=AsyncMock(side_effect=ConnectionError("not_open")),
    )
    record = SimpleNamespace(
        sync_id="s",
        space_id="sp",
        rtc=rtc_session,
        rtc_watcher=None,
    )
    svc._sync_manager = MagicMock()
    svc._sync_manager.apply_offer = AsyncMock(return_value="sdp-ans")
    svc._sync_manager.get_session = MagicMock(return_value=record)
    svc._space_sync_receiver = SimpleNamespace(on_chunk=AsyncMock())
    with patch.object(
        FederationService,
        "send_event",
        new_callable=AsyncMock,
    ) as send_mock:
        await svc._handle_space_sync_offer(
            _event(
                "SPACE_SYNC_OFFER",
                {"sync_id": "s", "sdp_offer": "x"},
                space_id="sp",
            ),
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        sent_events = [c.kwargs["event_type"].value for c in send_mock.await_args_list]
    assert "space_sync_direct_failed" in sent_events
    failed_call = next(
        c
        for c in send_mock.await_args_list
        if c.kwargs["event_type"].value == "space_sync_direct_failed"
    )
    assert failed_call.kwargs["payload"]["reason"] == "ice_timeout"


async def test_offer_handler_ice_timeout_emits_relay_begin_when_peer_supports(svc):
    """Requester-side ICE timeout sends DIRECT_FAILED (for the
    provider's cleanup) AND locally re-issues the BEGIN with
    ``prefer_direct=False``. Gated on the provider supporting v_13+
    so older peers don't get a BEGIN they can't honour."""
    rtc_session = SimpleNamespace(
        wait_ready=AsyncMock(return_value=False),
        recv_chunk=AsyncMock(side_effect=ConnectionError("not_open")),
    )
    record = SimpleNamespace(
        sync_id="s",
        space_id="sp",
        rtc=rtc_session,
        rtc_watcher=None,
    )
    svc._sync_manager = MagicMock()
    svc._sync_manager.apply_offer = AsyncMock(return_value="sdp-ans")
    svc._sync_manager.get_session = MagicMock(return_value=record)
    svc._sync_manager.trigger_relay_sync = AsyncMock(
        return_value=SimpleNamespace(
            next_event=MagicMock(value="space_sync_begin"),
            next_payload={
                "sync_id": "s-new",
                "space_id": "sp",
                "prefer_direct": False,
            },
        ),
    )
    svc._space_sync_receiver = SimpleNamespace(on_chunk=AsyncMock())
    with (
        patch.object(
            FederationService,
            "send_event",
            new_callable=AsyncMock,
        ) as send_mock,
        patch.object(
            FederationService,
            "peer_supports",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await svc._handle_space_sync_offer(
            _event(
                "SPACE_SYNC_OFFER",
                {"sync_id": "s", "sdp_offer": "x"},
                space_id="sp",
            ),
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        sent = [c.kwargs["event_type"] for c in send_mock.await_args_list]
    # Order: ANSWER (immediate) → DIRECT_FAILED + relay BEGIN (from watcher).
    sent_values = [getattr(e, "value", e) for e in sent]
    assert "space_sync_direct_failed" in sent_values
    assert "space_sync_begin" in sent_values
    svc._sync_manager.trigger_relay_sync.assert_awaited_once_with("s")


async def test_offer_handler_ice_timeout_skips_relay_for_old_peer(svc):
    """When the provider doesn't advertise v_13, the requester only
    emits DIRECT_FAILED (so the provider cleans up) and closes its
    own session — without sending a BEGIN the older provider would
    accept and silently ignore."""
    rtc_session = SimpleNamespace(
        wait_ready=AsyncMock(return_value=False),
        recv_chunk=AsyncMock(side_effect=ConnectionError("not_open")),
    )
    record = SimpleNamespace(
        sync_id="s",
        space_id="sp",
        rtc=rtc_session,
        rtc_watcher=None,
    )
    svc._sync_manager = MagicMock()
    svc._sync_manager.apply_offer = AsyncMock(return_value="sdp-ans")
    svc._sync_manager.get_session = MagicMock(return_value=record)
    svc._sync_manager.trigger_relay_sync = AsyncMock()
    svc._sync_manager.close_session = MagicMock()
    svc._space_sync_receiver = SimpleNamespace(on_chunk=AsyncMock())
    with (
        patch.object(
            FederationService,
            "send_event",
            new_callable=AsyncMock,
        ) as send_mock,
        patch.object(
            FederationService,
            "peer_supports",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        await svc._handle_space_sync_offer(
            _event(
                "SPACE_SYNC_OFFER",
                {"sync_id": "s", "sdp_offer": "x"},
                space_id="sp",
            ),
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        sent_values = [c.kwargs["event_type"].value for c in send_mock.await_args_list]
    assert "space_sync_direct_failed" in sent_values
    assert "space_sync_begin" not in sent_values
    svc._sync_manager.trigger_relay_sync.assert_not_awaited()
    svc._sync_manager.close_session.assert_called_once_with("s")


# ─── Mesh-only requester: the sync helpers must not plain-send toward an
# unpaired provider (no ``remote_instances`` row → ``send_event`` logs
# "unknown instance" and drops the event with no outbox). ─────────────

LOGGER = "socialhome.federation.federation_service"


def _rtc_record(*, ready: bool):
    rtc_session = SimpleNamespace(
        wait_ready=AsyncMock(return_value=ready),
        recv_chunk=AsyncMock(side_effect=ConnectionError("eof")),
    )
    return SimpleNamespace(
        sync_id="s",
        space_id="sp",
        rtc=rtc_session,
        rtc_watcher=None,
        provider_instance_id="mesh-provider",
        requester_instance_id="self-iid",
    )


async def test_offer_from_unpaired_provider_is_skipped_with_debug_log(svc, caplog):
    """(b) — an OFFER from a provider we hold no pairing row for cannot be
    answered: ANSWER/ICE ride direct ``send_event`` and the host never
    offers a mesh requester anyway. Skip the whole dance at debug — no
    RTC session, no ANSWER, and no ``unknown instance`` warning."""
    _unpair(svc)
    svc._sync_manager = MagicMock()
    svc._sync_manager.apply_offer = AsyncMock(return_value="sdp-ans")
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    with patch.object(
        FederationService,
        "send_event",
        new_callable=AsyncMock,
    ) as send_mock:
        await svc._handle_space_sync_offer(
            _event(
                "SPACE_SYNC_OFFER",
                {"sync_id": "s", "sdp_offer": "x"},
                from_instance="mesh-provider",
                space_id="sp",
            ),
        )
    svc._sync_manager.apply_offer.assert_not_awaited()
    send_mock.assert_not_awaited()
    assert _unknown_instance_warnings(caplog) == []
    debug = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("mesh" in r.getMessage() for r in debug)


async def test_offer_from_confirmed_provider_answers_via_plain_send_event(svc):
    """Paired peer: the ANSWER goes out over plain ``send_event`` with the
    same payload as before — the (b) guard is invisible to it."""
    svc._sync_manager = MagicMock()
    svc._sync_manager.apply_offer = AsyncMock(return_value="sdp-ans")
    svc._sync_manager.get_session = MagicMock(return_value=None)
    with patch.object(
        FederationService,
        "send_event",
        new_callable=AsyncMock,
    ) as send_mock:
        await svc._handle_space_sync_offer(
            _event(
                "SPACE_SYNC_OFFER",
                {"sync_id": "s", "sdp_offer": "x"},
                from_instance="paired-provider",
                space_id="sp",
            ),
        )
    send_mock.assert_awaited_once_with(
        to_instance_id="paired-provider",
        event_type=FederationEventType.SPACE_SYNC_ANSWER,
        payload={"sync_id": "s", "sdp_answer": "sdp-ans"},
        space_id="sp",
    )


async def test_watcher_direct_ready_skipped_for_mesh_provider(svc, caplog):
    """(b) — DIRECT_READY only means something on the direct path (an open
    DataChannel); toward a mesh-only provider it is not sent at all."""
    _unpair(svc)
    send_routed = _attach_mesh(svc, target="mesh-provider")
    record = _rtc_record(ready=True)
    svc._space_sync_receiver = SimpleNamespace(on_chunk=AsyncMock())
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    with patch.object(
        FederationService,
        "send_event",
        new_callable=AsyncMock,
    ) as send_mock:
        await svc._watch_requester_rtc(record, "mesh-provider")
    send_mock.assert_not_awaited()
    send_routed.assert_not_awaited()
    assert _unknown_instance_warnings(caplog) == []
    assert any(
        r.levelno == logging.DEBUG and "DIRECT_READY" in r.getMessage()
        for r in caplog.records
    )


async def test_watcher_ice_timeout_routes_failed_and_begin_via_mesh(svc, caplog):
    """(a) — regression for the live four-household run: a mesh-only
    requester whose ICE watcher timed out used to plain-``send_event``
    DIRECT_FAILED and the relay BEGIN toward a provider it has no row
    for, logging ``send_event: unknown instance`` and losing both. Both
    legs now ride SPACE_ROUTED via ``send_with_mesh_fallback``."""
    _unpair(svc)
    send_routed = _attach_mesh(svc, target="mesh-provider")
    record = _rtc_record(ready=False)
    svc._sync_manager = MagicMock()
    svc._sync_manager.trigger_relay_sync = AsyncMock(
        return_value=SimpleNamespace(
            next_event=FederationEventType.SPACE_SYNC_BEGIN,
            next_payload={
                "sync_id": "s-new",
                "space_id": "sp",
                "prefer_direct": False,
            },
        ),
    )
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    with patch.object(
        FederationService,
        "peer_supports",
        new_callable=AsyncMock,
        return_value=True,
    ):
        # Real ``send_event`` on purpose — with no row it is exactly the
        # code path that logged the warning before the fix.
        await svc._watch_requester_rtc(record, "mesh-provider")
    assert _unknown_instance_warnings(caplog) == []
    inner = [
        (c.kwargs["inner_event_type"], c.kwargs["inner_payload"])
        for c in send_routed.await_args_list
    ]
    assert inner == [
        (
            FederationEventType.SPACE_SYNC_DIRECT_FAILED,
            {"sync_id": "s", "reason": "ice_timeout"},
        ),
        (
            FederationEventType.SPACE_SYNC_BEGIN,
            {"sync_id": "s-new", "space_id": "sp", "prefer_direct": False},
        ),
    ]


async def test_watcher_ice_timeout_confirmed_provider_uses_plain_send_event(svc):
    """Paired peer: DIRECT_FAILED + relay BEGIN still go out over plain
    ``send_event`` (the short-circuit) — the mesh is never touched."""
    send_routed = _attach_mesh(svc, target="paired-provider")
    record = _rtc_record(ready=False)
    svc._sync_manager = MagicMock()
    svc._sync_manager.trigger_relay_sync = AsyncMock(
        return_value=SimpleNamespace(
            next_event=FederationEventType.SPACE_SYNC_BEGIN,
            next_payload={
                "sync_id": "s-new",
                "space_id": "sp",
                "prefer_direct": False,
            },
        ),
    )
    with (
        patch.object(
            FederationService,
            "send_event",
            new_callable=AsyncMock,
        ) as send_mock,
        patch.object(
            FederationService,
            "peer_supports",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await svc._watch_requester_rtc(record, "paired-provider")
    send_routed.assert_not_awaited()
    svc._route_service.discover_route.assert_not_awaited()
    assert [c.kwargs for c in send_mock.await_args_list] == [
        {
            "to_instance_id": "paired-provider",
            "event_type": FederationEventType.SPACE_SYNC_DIRECT_FAILED,
            "payload": {"sync_id": "s", "reason": "ice_timeout"},
            "space_id": "sp",
        },
        {
            "to_instance_id": "paired-provider",
            "event_type": FederationEventType.SPACE_SYNC_BEGIN,
            "payload": {"sync_id": "s-new", "space_id": "sp", "prefer_direct": False},
            "space_id": "sp",
        },
    ]


async def test_maybe_trigger_relay_retry_routes_begin_via_mesh(svc, caplog):
    """(a) — the re-BEGIN toward a mesh-only provider rides SPACE_ROUTED;
    no ``unknown instance`` warning."""
    _unpair(svc)
    send_routed = _attach_mesh(svc, target="mesh-provider")
    svc._sync_manager = MagicMock()
    svc._sync_manager.trigger_relay_sync = AsyncMock(
        return_value=SimpleNamespace(
            next_event=FederationEventType.SPACE_SYNC_BEGIN,
            next_payload={"sync_id": "s2", "space_id": "sp", "prefer_direct": False},
        ),
    )
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    with patch.object(
        FederationService,
        "peer_supports",
        new_callable=AsyncMock,
        return_value=True,
    ):
        await svc._maybe_trigger_relay_retry("s", "mesh-provider")
    assert _unknown_instance_warnings(caplog) == []
    send_routed.assert_awaited_once()
    kwargs = send_routed.await_args.kwargs
    assert kwargs["inner_event_type"] is FederationEventType.SPACE_SYNC_BEGIN
    assert kwargs["inner_payload"] == {
        "sync_id": "s2",
        "space_id": "sp",
        "prefer_direct": False,
    }


async def test_handle_direct_failed_requester_routes_begin_via_mesh(svc, caplog):
    """(a) — a mesh-only requester told DIRECT_FAILED by its provider
    re-issues the relay BEGIN over SPACE_ROUTED, not a doomed plain send."""
    _unpair(svc)
    send_routed = _attach_mesh(svc, target="mesh-provider")
    session = SimpleNamespace(
        sync_id="s1",
        signaling_node=None,
        provider_instance_id="mesh-provider",
    )
    svc._sync_manager = MagicMock()
    svc._sync_manager.get_session = MagicMock(return_value=session)
    svc._sync_manager.trigger_relay_sync = AsyncMock(
        return_value=SimpleNamespace(
            next_event=FederationEventType.SPACE_SYNC_BEGIN,
            next_payload={"sync_id": "s1b", "space_id": "sp", "prefer_direct": False},
        ),
    )
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    await svc._handle_space_sync_direct_failed(
        _event(
            "SPACE_SYNC_DIRECT_FAILED",
            {"sync_id": "s1"},
            from_instance="mesh-provider",
            space_id="sp",
        ),
    )
    assert _unknown_instance_warnings(caplog) == []
    send_routed.assert_awaited_once()
    kwargs = send_routed.await_args.kwargs
    assert kwargs["inner_event_type"] is FederationEventType.SPACE_SYNC_BEGIN
    assert kwargs["inner_payload"]["sync_id"] == "s1b"


async def test_handle_direct_failed_requester_confirmed_uses_plain_send_event(svc):
    """Paired peer: same BEGIN, same payload, over plain ``send_event``."""
    send_routed = _attach_mesh(svc, target="paired-provider")
    session = SimpleNamespace(
        sync_id="s1",
        signaling_node=None,
        provider_instance_id="paired-provider",
    )
    svc._sync_manager = MagicMock()
    svc._sync_manager.get_session = MagicMock(return_value=session)
    svc._sync_manager.trigger_relay_sync = AsyncMock(
        return_value=SimpleNamespace(
            next_event=FederationEventType.SPACE_SYNC_BEGIN,
            next_payload={"sync_id": "s1b", "space_id": "sp", "prefer_direct": False},
        ),
    )
    with patch.object(
        FederationService,
        "send_event",
        new_callable=AsyncMock,
    ) as send_mock:
        await svc._handle_space_sync_direct_failed(
            _event(
                "SPACE_SYNC_DIRECT_FAILED",
                {"sync_id": "s1"},
                from_instance="paired-provider",
                space_id="sp",
            ),
        )
    send_routed.assert_not_awaited()
    send_mock.assert_awaited_once_with(
        to_instance_id="paired-provider",
        event_type=FederationEventType.SPACE_SYNC_BEGIN,
        payload={"sync_id": "s1b", "space_id": "sp", "prefer_direct": False},
        space_id="sp",
    )


# ─── Part C: SPACE_SYNC_CHUNK inbound (HTTPS fallback) ─────────────


async def test_handle_space_sync_chunk_forwards_to_receiver(svc):
    """The HTTPS fallback ships chunks as signed ``SPACE_SYNC_CHUNK``
    federation events. The handler must forward the inner chunk
    body to the receiver, which runs the same signature +
    decryption pipeline RTC frames go through."""
    svc._sync_manager = MagicMock()
    svc._sync_manager.get_session = MagicMock(
        return_value=SimpleNamespace(provider_instance_id="peer-id"),
    )
    svc._space_sync_receiver = SimpleNamespace(on_chunk=AsyncMock())
    await svc._handle_space_sync_chunk(
        _event(
            "SPACE_SYNC_CHUNK",
            {"sync_id": "s", "chunk": "raw-bytes"},
            from_instance="peer-id",
        ),
    )
    svc._space_sync_receiver.on_chunk.assert_awaited_once_with(
        "raw-bytes",
        from_instance="peer-id",
    )


async def test_handle_space_sync_chunk_rejects_wrong_provider(svc):
    """The provider_instance_id on the session must match the
    envelope's from_instance — otherwise a paired peer could inject
    chunks into someone else's sync session."""
    svc._sync_manager = MagicMock()
    svc._sync_manager.get_session = MagicMock(
        return_value=SimpleNamespace(provider_instance_id="legit-peer"),
    )
    svc._space_sync_receiver = SimpleNamespace(on_chunk=AsyncMock())
    await svc._handle_space_sync_chunk(
        _event(
            "SPACE_SYNC_CHUNK",
            {"sync_id": "s", "chunk": "raw"},
            from_instance="attacker",
        ),
    )
    svc._space_sync_receiver.on_chunk.assert_not_awaited()


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


async def test_handle_direct_ready_releases_signaling_node(svc):
    """DIRECT_READY decrements the GFS counter (§24.10.7)."""
    session = SimpleNamespace(
        sync_id="s1",
        requester_instance_id="peer-1",
        signaling_node="https://b.gfs.test",
    )
    svc._sync_manager = MagicMock()
    svc._sync_manager.get_session = MagicMock(return_value=session)
    svc._space_sync_service = MagicMock()
    svc._space_sync_service.stream_initial = AsyncMock()
    svc._gfs_connection_service = MagicMock()
    svc._gfs_connection_service.release_signaling_node = AsyncMock()
    await svc._handle_space_sync_direct_ready(
        _event("SPACE_SYNC_DIRECT_READY", {"sync_id": "s1"}),
    )
    svc._gfs_connection_service.release_signaling_node.assert_awaited_once()
    # Session's signaling_node is cleared so a duplicate release is a no-op.
    assert session.signaling_node is None


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
    svc._sync_manager.get_session = MagicMock(return_value=None)
    svc._sync_manager.trigger_relay_sync = AsyncMock(
        return_value=SimpleNamespace(next_event=None, next_payload=None),
    )
    await svc._handle_space_sync_direct_failed(
        _event("SPACE_SYNC_DIRECT_FAILED", {"sync_id": "s1"}),
    )
    svc._sync_manager.trigger_relay_sync.assert_awaited_once()


async def test_handle_direct_failed_releases_signaling_node(svc):
    """DIRECT_FAILED also decrements the GFS counter (§24.10.7)."""
    session = SimpleNamespace(
        sync_id="s1",
        signaling_node="https://b.gfs.test",
        # Provider-side cleanup branch: local is the requester here,
        # so the existing relay-retry path runs (provider_instance_id
        # is the peer, not us).
        provider_instance_id="other-iid",
    )
    svc._sync_manager = MagicMock()
    svc._sync_manager.get_session = MagicMock(return_value=session)
    svc._sync_manager.trigger_relay_sync = AsyncMock(
        return_value=SimpleNamespace(next_event=None, next_payload=None),
    )
    svc._gfs_connection_service = MagicMock()
    svc._gfs_connection_service.release_signaling_node = AsyncMock()
    await svc._handle_space_sync_direct_failed(
        _event("SPACE_SYNC_DIRECT_FAILED", {"sync_id": "s1"}),
    )
    svc._gfs_connection_service.release_signaling_node.assert_awaited_once()


async def test_handle_direct_failed_as_provider_skips_retry(svc):
    """When local is the provider for the sync_id (the requester sent
    DIRECT_FAILED on their ICE timeout), the handler MUST close the
    session and stop — bouncing a fresh BEGIN at the requester from
    this side is the wrong direction."""
    session = SimpleNamespace(
        sync_id="s1",
        signaling_node=None,
        provider_instance_id="self-iid",  # local IS the provider
    )
    svc._sync_manager = MagicMock()
    svc._sync_manager.get_session = MagicMock(return_value=session)
    svc._sync_manager.trigger_relay_sync = AsyncMock()
    svc._sync_manager.close_session = MagicMock()
    with patch.object(
        FederationService,
        "send_event",
        new_callable=AsyncMock,
    ) as send_mock:
        await svc._handle_space_sync_direct_failed(
            _event("SPACE_SYNC_DIRECT_FAILED", {"sync_id": "s1"}),
        )
    svc._sync_manager.close_session.assert_called_once_with("s1")
    svc._sync_manager.trigger_relay_sync.assert_not_awaited()
    send_mock.assert_not_awaited()


async def test_release_signaling_node_idempotent(svc):
    """Second call after a session.signaling_node clear is a no-op."""
    session = SimpleNamespace(sync_id="s1", signaling_node=None)
    svc._gfs_connection_service = MagicMock()
    svc._gfs_connection_service.release_signaling_node = AsyncMock()
    await svc._release_signaling_node(session)
    svc._gfs_connection_service.release_signaling_node.assert_not_awaited()


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


# ── #648: the host re-probes before streaming to a mesh requester ─────


async def test_mesh_begin_invalidates_cached_route_before_streaming(svc):
    """A mesh BEGIN drops the host's cached route to the requester first.

    Every chunk of the stream is sealed under the ``target_eph_pk`` the
    host's route cache holds, but the private half only ever lived in the
    requester's RAM. If the requester restarted, that pub is dead and the
    host would seal the whole stream under it — chunks silently dropped on
    arrival, metadata lost for good (the media path self-heals via its
    durable outbox; the one-shot metadata stream does not).

    A BEGIN is proof the requester is alive *now*, so the cached route is
    dropped and the first chunk re-probes for a full-TTL key minted by the
    requester's current process.
    """
    record = SimpleNamespace(sync_id="m2", rtc=None, transport_mode="rtc")
    svc._sync_manager = MagicMock()
    svc._sync_manager.begin_session = AsyncMock(
        return_value=SimpleNamespace(
            accepted=True,
            next_event=None,
            next_payload=None,
        ),
    )
    svc._sync_manager.get_session = MagicMock(return_value=record)
    svc._space_sync_service = MagicMock()
    svc._space_sync_service.stream_initial = AsyncMock()
    svc._route_service = MagicMock()
    svc._route_service.invalidate_if_older_than = AsyncMock(return_value=True)

    with patch.object(
        FederationService,
        "is_confirmed_peer",
        new_callable=AsyncMock,
        return_value=False,
    ):
        await svc._handle_space_sync_begin(
            _event(
                "SPACE_SYNC_BEGIN",
                {"sync_id": "m2", "space_id": "sp", "prefer_direct": False},
                space_id="sp",
            ),
        )
        await asyncio.sleep(0)

    svc._route_service.invalidate_if_older_than.assert_awaited_once()
    assert svc._route_service.invalidate_if_older_than.await_args.args[0] == "peer-1"
    svc._space_sync_service.stream_initial.assert_awaited_once_with(record)


async def test_confirmed_peer_begin_leaves_route_cache_alone(svc):
    """A CONFIRMED requester's BEGIN must not force a re-probe.

    Its chunks go out over the direct path, which never touches a target
    ephemeral, so invalidating would spend a pointless BFS per BEGIN.
    """
    record = SimpleNamespace(sync_id="m3", rtc=None, transport_mode="rtc")
    svc._sync_manager = MagicMock()
    svc._sync_manager.begin_session = AsyncMock(
        return_value=SimpleNamespace(
            accepted=True,
            next_event=None,
            next_payload=None,
        ),
    )
    svc._sync_manager.get_session = MagicMock(return_value=record)
    svc._space_sync_service = MagicMock()
    svc._space_sync_service.stream_initial = AsyncMock()
    svc._route_service = MagicMock()
    svc._route_service.invalidate_if_older_than = AsyncMock(return_value=True)

    with patch.object(
        FederationService,
        "is_confirmed_peer",
        new_callable=AsyncMock,
        return_value=True,
    ):
        await svc._handle_space_sync_begin(
            _event(
                "SPACE_SYNC_BEGIN",
                # prefer_direct=False → still the HTTPS branch, but the
                # requester is a confirmed peer.
                {"sync_id": "m3", "space_id": "sp", "prefer_direct": False},
                space_id="sp",
            ),
        )
        await asyncio.sleep(0)

    svc._route_service.invalidate_if_older_than.assert_not_awaited()
    svc._space_sync_service.stream_initial.assert_awaited_once_with(record)


async def test_mesh_begin_survives_unwired_route_service(svc):
    """No mesh attached → the stream still starts, no AttributeError."""
    record = SimpleNamespace(sync_id="m4", rtc=None, transport_mode="rtc")
    svc._sync_manager = MagicMock()
    svc._sync_manager.begin_session = AsyncMock(
        return_value=SimpleNamespace(
            accepted=True,
            next_event=None,
            next_payload=None,
        ),
    )
    svc._sync_manager.get_session = MagicMock(return_value=record)
    svc._space_sync_service = MagicMock()
    svc._space_sync_service.stream_initial = AsyncMock()
    svc._route_service = None

    with patch.object(
        FederationService,
        "is_confirmed_peer",
        new_callable=AsyncMock,
        return_value=False,
    ):
        await svc._handle_space_sync_begin(
            _event(
                "SPACE_SYNC_BEGIN",
                {"sync_id": "m4", "space_id": "sp", "prefer_direct": False},
                space_id="sp",
            ),
        )
        await asyncio.sleep(0)

    svc._space_sync_service.stream_initial.assert_awaited_once_with(record)
