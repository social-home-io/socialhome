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


def test_location_update_zone_asserted_by_default():
    """A caller that doesn't say otherwise has stated its zone (``None`` =
    not in a zone) — the server only matches GPS when told the client
    stated nothing."""
    assert LocationUpdate(username="a", state="away").zone_asserted is True
    lu = LocationUpdate(username="a", state="away", zone_asserted=False)
    assert lu.zone_asserted is False
