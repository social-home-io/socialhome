"""Tests for socialhome.domain.federation."""

from __future__ import annotations

from socialhome.domain.federation import (
    DELIVERY_ERROR_QUEUED,
    BroadcastResult,
    DecryptedPayload,
    DeliveryResult,
    FederationEnvelope,
    FederationEvent,
    FederationEventType,
    PAIRING_EVENTS,
    STRUCTURAL_EVENTS,
)


def test_broadcast_result_all_ok():
    """BroadcastResult.all_ok is True iff all deliveries succeeded."""
    r1 = BroadcastResult(attempted=2, succeeded=2, failed=0)
    assert r1.all_ok
    r2 = BroadcastResult(attempted=2, succeeded=1, failed=1)
    assert not r2.all_ok
    r3 = BroadcastResult(attempted=0, succeeded=0, failed=0)
    assert not r3.all_ok


def test_delivery_error_queued_keeps_wire_value():
    """The constant names the outbox-queued reason; the log/wire literal
    stays ``"delivery_failed"`` so existing consumers keep matching."""
    assert DELIVERY_ERROR_QUEUED == "delivery_failed"


def test_broadcast_result_terminal_failures_excludes_queued():
    """``terminal_failures`` is the mesh-path subset of ``ok=False``: an
    outbox-queued direct-peer failure self-heals and is NOT a loss, while
    ``failed`` still counts every ``ok=False`` result."""
    queued = DeliveryResult(instance_id="direct", ok=False, error=DELIVERY_ERROR_QUEUED)
    lost = DeliveryResult(instance_id="mesh", ok=False, error="no_route")
    fine = DeliveryResult(instance_id="ok", ok=True)
    r = BroadcastResult(
        attempted=3, succeeded=1, failed=2, results=(queued, lost, fine)
    )
    assert r.failed == 2
    assert r.terminal_failures == (lost,)
    assert not r.all_ok

    only_queued = BroadcastResult(attempted=1, succeeded=0, failed=1, results=(queued,))
    assert only_queued.failed == 1
    assert only_queued.terminal_failures == ()


def test_delivery_result():
    """DeliveryResult carries instance_id and ok flag."""
    d = DeliveryResult(instance_id="p1", ok=True, status_code=200)
    assert d.ok


def test_federation_event():
    """FederationEvent carries the validated payload and space_id."""
    e = FederationEvent(
        msg_id="m1",
        event_type=FederationEventType.SPACE_POST_CREATED,
        from_instance="a",
        to_instance="b",
        timestamp="now",
        payload={"key": "val"},
        space_id="s1",
    )
    assert e.space_id == "s1"


def test_decrypted_payload():
    """DecryptedPayload wraps event_type and a payload dict."""
    dp = DecryptedPayload(
        event_type=FederationEventType.DM_MESSAGE,
        payload={"x": 1},
    )
    assert dp.payload["x"] == 1


def test_federation_envelope():
    """FederationEnvelope exposes routing fields and proto_version=1."""
    env = FederationEnvelope(
        msg_id="m1",
        event_type=FederationEventType.PAIRING_ACCEPT,
        from_instance="a",
        to_instance="b",
        timestamp="now",
        encrypted_payload="enc",
        signature="sig",
    )
    assert env.proto_version == 1


def test_pairing_events_subset():
    """PAIRING_EVENTS contains pairing types but not structural ones."""
    assert FederationEventType.PAIRING_INTRO in PAIRING_EVENTS
    assert FederationEventType.SPACE_POST_CREATED not in PAIRING_EVENTS


def test_structural_events_subset():
    """STRUCTURAL_EVENTS contains SPACE_CREATED."""
    assert FederationEventType.SPACE_CREATED in STRUCTURAL_EVENTS


def test_app_session_event_type_round_trip():
    """APP_SESSION wire value round-trips through the str enum."""
    assert FederationEventType("app_session") == FederationEventType.APP_SESSION
    assert FederationEventType.APP_SESSION == "app_session"


def test_app_message_event_type_round_trip():
    """APP_MESSAGE wire value round-trips through the str enum."""
    assert FederationEventType("app_message") == FederationEventType.APP_MESSAGE
    assert FederationEventType.APP_MESSAGE == "app_message"


def test_space_sync_rejected_event_type_round_trip():
    """SPACE_SYNC_REJECTED wire value round-trips through the str enum."""
    assert (
        FederationEventType("space_sync_rejected")
        == FederationEventType.SPACE_SYNC_REJECTED
    )
    assert FederationEventType.SPACE_SYNC_REJECTED == "space_sync_rejected"


def test_space_route_stale_event_type_round_trip():
    """SPACE_ROUTE_STALE wire value round-trips through the str enum.

    Membership in the enum is what makes the §24.11 validator accept
    the type — see ``tests/federation/test_inbound_validator.py``.
    """
    assert (
        FederationEventType("space_route_stale")
        == FederationEventType.SPACE_ROUTE_STALE
    )
    assert FederationEventType.SPACE_ROUTE_STALE == "space_route_stale"
