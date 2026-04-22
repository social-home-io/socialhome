"""Tests for socialhome.domain.presence."""

from __future__ import annotations

from socialhome.domain.presence import (
    LocationUpdate,
    PersonPresence,
    PRESENCE_STATES,
    truncate_coord,
)


def test_truncate():
    """truncate_coord keeps exactly 4 decimal places."""
    assert truncate_coord(52.37654321) == 52.3765


def test_states():
    """PRESENCE_STATES contains expected values like 'home' and 'unavailable'."""
    assert "home" in PRESENCE_STATES
    assert "unavailable" in PRESENCE_STATES


def test_location_update_fields():
    """LocationUpdate carries GPS coordinate fields."""
    lu = LocationUpdate(username="a", state="home", latitude=52.37, longitude=4.89)
    assert lu.latitude == 52.37


def test_person_presence_fields():
    """PersonPresence carries display and zone fields."""
    pp = PersonPresence(
        username="a",
        user_id="u1",
        display_name="A",
        entity_id="person.a",
        state="home",
        zone_name="Home",
    )
    assert pp.zone_name == "Home"
