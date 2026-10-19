"""A global space's place pin — the pure rules (``domain/space_place``)."""

from __future__ import annotations

import pytest

from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.domain.space_place import (
    PlacePin,
    parse_place_pin_wire,
    place_pin_of,
    place_pin_wire,
    validate_place_pin,
)


def _space(**over) -> Space:
    base = {
        "id": "sp",
        "name": "Chess Zürich",
        "owner_instance_id": "own",
        "owner_username": "anna",
        "identity_public_key": "aa" * 32,
        "config_sequence": 1,
        "features": SpaceFeatures(),
        "space_type": SpaceType.GLOBAL,
        "join_mode": JoinMode.REQUEST,
        "lat": 47.3769,
        "lon": 8.5417,
    }
    base.update(over)
    return Space(**base)


def test_validate_truncates_to_4dp():
    assert validate_place_pin({"lat": 47.376_912_3, "lon": -8.541_749_9}) == PlacePin(
        47.3769, -8.5417
    )


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        "47,8",
        {"lat": 47.0},
        {"lat": "47", "lon": 8},
        {"lat": True, "lon": 8},
        {"lat": 91, "lon": 8},
        {"lat": 47, "lon": 181},
        {"lat": float("nan"), "lon": 8},
    ],
)
def test_validate_refuses_malformed(raw):
    with pytest.raises(ValueError):
        validate_place_pin(raw)


def test_only_a_global_space_has_a_pin():
    assert place_pin_of(_space()) == PlacePin(47.3769, 8.5417)
    for tier in (SpaceType.PUBLIC, SpaceType.PRIVATE, SpaceType.HOUSEHOLD):
        assert place_pin_of(_space(space_type=tier)) is None


def test_no_pin_is_no_pin():
    assert place_pin_of(_space(lat=None, lon=None)) is None
    # A radius alone is no pin.
    assert place_pin_of(_space(lat=None, lon=None, radius_km=5.0)) is None


def test_a_global_pin_carries_its_radius_hint():
    """On a GLOBAL space ``radius_km`` is the owner's hint ("best for people
    within 10 km") — never enforced; on a public space the same column is a
    local community's area, which is never a pin."""
    assert place_pin_of(_space(radius_km=10.0)) == PlacePin(47.3769, 8.5417, 10.0)
    assert place_pin_of(_space(space_type=SpaceType.PUBLIC, radius_km=10.0)) is None


def test_validate_takes_an_optional_radius_hint():
    assert validate_place_pin({"lat": 47.0, "lon": 8.0, "radius_km": 10}) == PlacePin(
        47.0, 8.0, 10.0
    )
    assert validate_place_pin({"lat": 47.0, "lon": 8.0, "radius_km": 2.345}) == (
        PlacePin(47.0, 8.0, 2.3)
    )
    # Absent or null: no hint.
    assert validate_place_pin({"lat": 47.0, "lon": 8.0, "radius_km": None}) == (
        PlacePin(47.0, 8.0)
    )


@pytest.mark.parametrize("bad", [0.4, 50.5, 0, -1, "10", True, float("inf")])
def test_a_radius_hint_out_of_bounds_is_refused(bad):
    with pytest.raises(ValueError):
        validate_place_pin({"lat": 47.0, "lon": 8.0, "radius_km": bad})


def test_the_wire_names_the_hint_only_when_there_is_one():
    """An unhinted pin is byte-for-byte what it was before the hint
    existed, so its signed publish body is unchanged."""
    assert place_pin_wire(PlacePin(47.0, 8.0)) == {"lat": 47.0, "lon": 8.0}
    assert place_pin_wire(PlacePin(47.0, 8.0, 10.0)) == {
        "lat": 47.0,
        "lon": 8.0,
        "radius_km": 10.0,
    }


def test_a_bad_hint_on_receipt_keeps_the_pin():
    """The hint only ever labels: a malformed one is dropped, the pin is
    kept for sorting."""
    assert parse_place_pin_wire({"lat": 47.0, "lon": 8.0, "radius_km": 999}) == (
        PlacePin(47.0, 8.0)
    )
    assert parse_place_pin_wire({"lat": 47.0, "lon": 8.0, "radius_km": 5}) == (
        PlacePin(47.0, 8.0, 5.0)
    )


def test_wire_round_trip_and_bad_wire_is_unpinned():
    pin = PlacePin(47.3769, 8.5417)
    assert parse_place_pin_wire(place_pin_wire(pin)) == pin
    assert parse_place_pin_wire(None) is None
    assert parse_place_pin_wire({"lat": 100, "lon": 0}) is None
    assert parse_place_pin_wire("x") is None
