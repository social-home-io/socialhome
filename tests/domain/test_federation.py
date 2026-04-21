"""Tests for social_home.domain.federation."""

from __future__ import annotations

from social_home.domain.federation import (
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
