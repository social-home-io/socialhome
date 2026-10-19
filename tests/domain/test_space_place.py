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


def test_no_pin_or_an_area_is_no_pin():
    assert place_pin_of(_space(lat=None, lon=None)) is None
    # A radius is a local community's area, never a pin.
    assert place_pin_of(_space(radius_km=5.0)) is None


def test_wire_round_trip_and_bad_wire_is_unpinned():
    pin = PlacePin(47.3769, 8.5417)
    assert parse_place_pin_wire(place_pin_wire(pin)) == pin
    assert parse_place_pin_wire(None) is None
    assert parse_place_pin_wire({"lat": 100, "lon": 0}) is None
    assert parse_place_pin_wire("x") is None
