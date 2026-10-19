"""Local communities — the pure area rules (``domain/local_community``)."""

from __future__ import annotations

import math
import random

import pytest

from socialhome.domain.local_community import (
    JOIN_LOCATION_PRECISION_DP,
    MAX_LOCAL_AREA_RADIUS_KM,
    MIN_LOCAL_AREA_RADIUS_KM,
    Eligibility,
    LocalArea,
    PeerLocalCommunity,
    coarse_join_point,
    join_location_wire,
    join_verdict,
    local_area_of,
    local_area_wire,
    local_community_wire,
    parse_join_location,
    parse_local_community_wire,
    validate_local_area,
)
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.domain.zone_geo import haversine_m

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


# ── the join's coarse point ─────────────────────────────────────────────


def test_the_join_precision_is_exactly_two_decimals():
    """~1.1 km: a part of town, never a house or a street."""
    assert JOIN_LOCATION_PRECISION_DP == 2
    assert coarse_join_point(47.376_912_3, 8.541_749_9) == (47.38, 8.54)
    assert join_location_wire((47.376_912_3, -8.546)) == {"lat": 47.38, "lon": -8.55}


@pytest.mark.parametrize(
    "raw,point",
    [
        ({"lat": 47.38, "lon": 8.54}, (47.38, 8.54)),
        # A sender that sent more is never read more precisely.
        ({"lat": 47.376_912_3, "lon": 8.541_749_9}, (47.38, 8.54)),
        ({"lat": 0, "lon": -0.001}, (0.0, 0.0)),
    ],
)
def test_a_received_point_is_rounded_again(raw, point):
    assert parse_join_location(raw) == point


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "47.38,8.54",
        [],
        {},
        {"lat": 47.38},
        {"lat": "47.38", "lon": 8.54},
        {"lat": True, "lon": 8.54},
        {"lat": 91, "lon": 8.54},
        {"lat": 47.38, "lon": 181},
        {"lat": math.nan, "lon": 8.54},
    ],
)
def test_a_malformed_point_is_no_point(raw):
    assert parse_join_location(raw) is None


def _east_km(km: float) -> tuple[float, float]:
    """The point ``km`` east of ZURICH on its parallel."""
    per_deg = 111.19492664455873 * math.cos(math.radians(ZURICH[0]))
    return ZURICH[0], ZURICH[1] + km / per_deg


AREA = LocalArea(ZURICH[0], ZURICH[1], 5.0)


def test_the_centre_is_inside():
    assert join_verdict(AREA, ZURICH) is Eligibility.INSIDE


def test_well_inside_and_well_outside():
    assert join_verdict(AREA, _east_km(2.0)) is Eligibility.INSIDE
    assert join_verdict(AREA, _east_km(8.0)) is Eligibility.OUTSIDE
    assert join_verdict(AREA, (46.9481, 7.4474)) is Eligibility.OUTSIDE  # Bern


@pytest.mark.parametrize("km", [4.95, 5.0, 5.3])
def test_a_cell_straddling_the_rim_is_the_edge(km):
    assert join_verdict(AREA, _east_km(km)) is Eligibility.EDGE


def test_the_verdict_holds_for_every_home_in_the_cell():
    """Inside means every home that rounds to the point is inside; outside
    means none is: the host never decides on more than the ~1 km cell."""
    rng = random.Random(7)
    seen = set()
    for _ in range(3000):
        home = (
            ZURICH[0] + rng.uniform(-0.08, 0.08),
            ZURICH[1] + rng.uniform(-0.12, 0.12),
        )
        verdict = join_verdict(AREA, coarse_join_point(*home))
        seen.add(verdict)
        inside = haversine_m(AREA.lat, AREA.lon, *home) <= AREA.radius_km * 1000
        if verdict is Eligibility.INSIDE:
            assert inside
        elif verdict is Eligibility.OUTSIDE:
            assert not inside
    assert seen == set(Eligibility)


def test_moving_a_home_within_its_cell_changes_nothing():
    a, b = (47.3751, 8.6049), (47.3849, 8.5951)
    assert coarse_join_point(*a) == coarse_join_point(*b)
    assert join_verdict(AREA, a) is join_verdict(AREA, b)


def test_the_verdict_works_across_the_antimeridian():
    area = LocalArea(0.0, 179.99, 5.0)
    assert join_verdict(area, (0.0, -179.99)) is not Eligibility.OUTSIDE
    assert join_verdict(area, (0.0, 179.0)) is Eligibility.OUTSIDE


# ── wire shapes ─────────────────────────────────────────────────────────


def test_the_api_shape_names_the_centre():
    assert local_area_wire(AREA) == {
        "lat": ZURICH[0],
        "lon": ZURICH[1],
        "radius_km": 5.0,
    }


def test_the_directory_shape_is_the_radius_only():
    wire = local_community_wire(AREA)
    assert wire == {"local_community": True, "radius_km": 5.0}
    assert parse_local_community_wire(wire) == PeerLocalCommunity(5.0)


def test_a_sent_verdict_or_centre_is_ignored_on_receipt():
    got = parse_local_community_wire(
        {
            "local_community": True,
            "radius_km": 5,
            "in_area": True,
            "area_edge": True,
            "lat": 47.0,
            "lon": 8.0,
        }
    )
    assert got == PeerLocalCommunity(5.0)


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


def test_the_radius_is_rounded_again_on_receipt():
    got = parse_local_community_wire({"local_community": True, "radius_km": 2.345})
    assert got == PeerLocalCommunity(2.3)
