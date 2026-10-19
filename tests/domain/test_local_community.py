"""Local communities — the pure area rules (``domain/local_community``)."""

from __future__ import annotations

import math

import pytest

from socialhome.domain.local_community import (
    MAX_LOCAL_AREA_RADIUS_KM,
    MIN_LOCAL_AREA_RADIUS_KM,
    Eligibility,
    LocalArea,
    PeerLocalCommunity,
    eligibility,
    local_area_of,
    local_area_wire,
    local_community_wire,
    parse_local_community_wire,
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


# ── wire shapes ─────────────────────────────────────────────────────────


def test_the_api_shape_names_the_centre():
    """Only the hosting household's own API carries the centre."""
    area = LocalArea(47.3769, 8.5417, 5.0)
    assert local_area_wire(area) == {"lat": 47.3769, "lon": 8.5417, "radius_km": 5.0}


@pytest.mark.parametrize(
    "verdict,in_area",
    [
        (Eligibility.INSIDE, True),
        (Eligibility.OUTSIDE, False),
        (Eligibility.HOME_UNKNOWN, None),
    ],
)
def test_the_directory_shape_carries_no_coordinates(verdict, in_area):
    """A paired household learns the radius and the host's verdict for
    ITS OWN home — never the centre."""
    wire = local_community_wire(LocalArea(47.3769, 8.5417, 5.0), verdict)
    assert wire == {"local_community": True, "radius_km": 5.0, "in_area": in_area}
    assert not {"lat", "lon", "latitude", "longitude", "local_area"} & set(wire)


@pytest.mark.parametrize("in_area", [True, False, None])
def test_directory_shape_round_trip(in_area):
    wire = {"local_community": True, "radius_km": 2.0, "in_area": in_area}
    assert parse_local_community_wire(wire) == PeerLocalCommunity(2.0, in_area)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "x",
        [],
        {},
        {"radius_km": 5},  # no flag
        {"local_community": "yes", "radius_km": 5},
        {"local_community": True},
        {"local_community": True, "radius_km": 500},
        {"local_community": True, "radius_km": 0.1},
        {"local_community": True, "radius_km": True},
        {"local_community": True, "radius_km": "5"},
        # The pre-rework shape: a centre is never read, whatever it says.
        {"local_area": {"lat": 47.3769, "lon": 8.5417, "radius_km": 5.0}},
    ],
)
def test_a_malformed_directory_entry_is_no_local_community(raw):
    """A broken entry shows no area at all — never a guessed one."""
    assert parse_local_community_wire(raw) is None


@pytest.mark.parametrize("bad", ["yes", 1, 0, [], {}])
def test_a_non_boolean_verdict_reads_as_unknown(bad):
    """Only ``true`` / ``false`` are verdicts; anything else is "the host
    does not know" — the host checks again on every seat anyway."""
    got = parse_local_community_wire(
        {"local_community": True, "radius_km": 5, "in_area": bad}
    )
    assert got == PeerLocalCommunity(5.0, None)


def test_the_radius_is_rounded_again_on_receipt():
    got = parse_local_community_wire({"local_community": True, "radius_km": 2.345})
    assert got == PeerLocalCommunity(2.3, None)
