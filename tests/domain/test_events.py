"""Tests for socialhome.domain.events."""

from __future__ import annotations

from datetime import UTC, datetime

from socialhome.domain.events import (
    DomainEvent,
    HouseholdZonesChanged,
    LocalHomeLocationUpdated,
    PeerHomeChanged,
    PeerTransportChanged,
    PostCreated,
)
from socialhome.domain.post import Post, PostType


def test_post_created_is_domain_event():
    """PostCreated is a DomainEvent with a valid occurred_at timestamp."""
    now = datetime.now(UTC)
    p = Post(id="p1", author="u1", type=PostType.TEXT, created_at=now)
    e = PostCreated(post=p)
    assert isinstance(e, DomainEvent)
    assert e.occurred_at is not None


def test_peer_transport_changed_shape():
    """PeerTransportChanged has instance_id and transport fields."""
    e = PeerTransportChanged(instance_id="iid-1", transport="rtc")
    assert e.instance_id == "iid-1"
    assert e.transport == "rtc"

    e2 = PeerTransportChanged(instance_id="iid-2", transport="https")
    assert e2.transport == "https"


def test_local_home_location_updated_shape():
    """LocalHomeLocationUpdated is a DomainEvent with lat/lon fields."""
    e = LocalHomeLocationUpdated(latitude=52.52, longitude=13.40)
    assert isinstance(e, DomainEvent)
    assert e.latitude == 52.52
    assert e.longitude == 13.40
    assert e.occurred_at is not None


def test_peer_home_changed_shape():
    """PeerHomeChanged has instance_id, latitude, and longitude fields."""
    e = PeerHomeChanged(
        instance_id="iid-1",
        latitude=52.52,
        longitude=13.40,
    )
    assert isinstance(e, DomainEvent)
    assert e.instance_id == "iid-1"
    assert e.latitude == 52.52
    assert e.longitude == 13.40
    assert e.occurred_at is not None


def test_app_challenge_received_is_frozen_slots():
    from socialhome.domain.events import AppChallengeReceived

    e = AppChallengeReceived(
        app_id="chess",
        session_id="s1",
        to_user_id="u-local",
        from_display="Alice",
    )
    assert e.app_id == "chess"
    assert e.to_user_id == "u-local"
    assert e.from_display == "Alice"
    import dataclasses

    import pytest

    with pytest.raises(dataclasses.FrozenInstanceError):
        e.app_id = "x"  # type: ignore[misc]


def test_household_zones_changed_is_a_content_free_nudge():
    """HouseholdZonesChanged carries no zone data — clients refetch."""
    e = HouseholdZonesChanged()
    assert isinstance(e, DomainEvent)
    assert e.occurred_at is not None
    assert set(HouseholdZonesChanged.__dataclass_fields__) == {"occurred_at"}
