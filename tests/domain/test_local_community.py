"""Local communities — the pure area rules (``domain/local_community``)."""

from __future__ import annotations

import math

import pytest

from socialhome.domain.local_community import (
    MAX_LOCAL_AREA_RADIUS_KM,
    MIN_LOCAL_AREA_RADIUS_KM,
    Eligibility,
    LocalArea,
    eligibility,
    local_area_of,
    local_area_wire,
    parse_local_area_wire,
    validate_local_area,
)
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType

ZURICH = (47.3769, 8.5417)


def _space(**kw) -> Space:
    base = {
        "id": "s1",
        "name": "Nachbarn",
        "owner_instance_id": "host",
        "owner_username": "anna",
        "identity_public_key": "aa" * 32,
        "config_sequence": 0,
        "features": SpaceFeatures(),
        "space_type": SpaceType.PUBLIC,
        "join_mode": JoinMode.REQUEST,
        "lat": ZURICH[0],
        "lon": ZURICH[1],
        "radius_km": 5.0,
    }
    base.update(kw)
    return Space(**base)


# ── validate_local_area ─────────────────────────────────────────────────


def test_validate_rounds_centre_to_4dp_and_radius_to_one_decimal():
    area = validate_local_area(47.376_912_3, 8.541_749_9, 2.345)
    assert area == LocalArea(lat=47.3769, lon=8.5417, radius_km=2.3)


@pytest.mark.parametrize(
    "radius", [MIN_LOCAL_AREA_RADIUS_KM, 5, MAX_LOCAL_AREA_RADIUS_KM]
)
def test_validate_accepts_the_bounds(radius):
    assert validate_local_area(1.0, 2.0, radius).radius_km == float(radius)


@pytest.mark.parametrize(
    "lat,lon,radius",
    [
        (91, 0, 5),
        (0, 181, 5),
        (0, 0, MIN_LOCAL_AREA_RADIUS_KM - 0.1),
        (0, 0, MAX_LOCAL_AREA_RADIUS_KM + 0.1),
        (0, 0, math.nan),
        (0, 0, math.inf),
        (0, 0, True),
        (0, 0, "5"),
        ("x", 0, 5),
        (None, 0, 5),
        (0, 0, None),
    ],
)
def test_validate_refuses_bad_input(lat, lon, radius):
    with pytest.raises(ValueError):
        validate_local_area(lat, lon, radius)


# ── local_area_of ───────────────────────────────────────────────────────


def test_a_public_space_with_centre_and_radius_is_a_local_community():
    assert local_area_of(_space()) == LocalArea(ZURICH[0], ZURICH[1], 5.0)


@pytest.mark.parametrize(
    "tier", [SpaceType.PRIVATE, SpaceType.HOUSEHOLD, SpaceType.GLOBAL]
)
def test_only_the_public_tier_is_ever_a_local_community(tier):
    assert local_area_of(_space(space_type=tier)) is None


def test_a_pin_without_a_radius_is_no_area():
    """A pin stored before local communities existed (create used to take
    lat / lon only) is not an area: nobody is gated by it."""
    assert local_area_of(_space(radius_km=None)) is None
    assert local_area_of(_space(lat=None)) is None
    assert local_area_of(_space(lon=None)) is None


# ── eligibility ─────────────────────────────────────────────────────────


def test_a_home_inside_the_radius_is_eligible():
    area = LocalArea(ZURICH[0], ZURICH[1], 5.0)
    # ~2.2 km north-east of the centre.
    assert eligibility(area, (47.3920, 8.5600)) is Eligibility.INSIDE


def test_the_centre_itself_and_the_rim_are_inside():
    area = LocalArea(0.0, 0.0, 1.0)
    assert eligibility(area, (0.0, 0.0)) is Eligibility.INSIDE
    # 1 km along the equator is ~0.008993° of longitude.
    assert eligibility(area, (0.0, 0.0089)) is Eligibility.INSIDE
    assert eligibility(area, (0.0, 0.0091)) is Eligibility.OUTSIDE


def test_a_home_outside_the_radius_is_refused():
    area = LocalArea(ZURICH[0], ZURICH[1], 5.0)
    # Bern is ~95 km away.
    assert eligibility(area, (46.9481, 7.4474)) is Eligibility.OUTSIDE


def test_an_unknown_home_is_its_own_answer():
    area = LocalArea(ZURICH[0], ZURICH[1], 5.0)
    assert eligibility(area, None) is Eligibility.HOME_UNKNOWN


# ── wire shape ──────────────────────────────────────────────────────────


def test_wire_round_trip():
    area = LocalArea(47.3769, 8.5417, 5.0)
    wire = local_area_wire(area)
    assert wire == {"lat": 47.3769, "lon": 8.5417, "radius_km": 5.0}
    assert parse_local_area_wire(wire) == area


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "x",
        [],
        {"lat": 1, "lon": 2},
        {"lat": 1, "lon": 2, "radius_km": 500},
        {"lat": 100, "lon": 2, "radius_km": 5},
        {"lat": True, "lon": 2, "radius_km": 5},
    ],
)
def test_a_malformed_wire_area_is_dropped(raw):
    """A peer's directory entry with a broken area shows no area at all —
    never a guessed one."""
    assert parse_local_area_wire(raw) is None


def test_a_wire_area_is_truncated_again_on_receipt():
    """The receiver re-applies the 4 dp rule — it never stores more
    precision than the GPS rule allows, whatever the sender put there."""
    got = parse_local_area_wire(
        {"lat": 47.376_912_3, "lon": 8.541_7499, "radius_km": 5}
    )
    assert got == LocalArea(47.3769, 8.5417, 5.0)
