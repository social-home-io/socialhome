"""The household home location's pure rules."""

from __future__ import annotations

import math

import pytest

from socialhome.domain.home_location import (
    PEER_HOME_GRID_DEG,
    HomeLocation,
    HomeLocationFromPlatformError,
    InvalidHomeLocationError,
    coarse_home_for_peer,
    coarse_home_for_peer_or_none,
    validate_home_location,
)
from socialhome.domain.zone_geo import haversine_m


def test_rounds_to_4dp():
    assert validate_home_location(47.376912345, 8.541698765) == (47.3769, 8.5417)


def test_ints_are_fine_and_bounds_are_inclusive():
    assert validate_home_location(90, -180) == (90.0, -180.0)
    assert validate_home_location(-90, 180) == (-90.0, 180.0)


@pytest.mark.parametrize(
    ("lat", "lon"),
    [
        (90.0001, 0.5),
        (-90.5, 0.5),
        (10.0, 180.0001),
        (10.0, -181),
        (None, 8.5),
        (47.3, None),
        ("47.3", 8.5),
        (True, 8.5),
        (47.3, False),
        (math.nan, 8.5),
        (47.3, math.inf),
        (0, 0),
        (0.00001, -0.00002),  # rounds to 0, 0 — "not set"
    ],
)
def test_rejects_unusable_points(lat, lon):
    with pytest.raises(InvalidHomeLocationError) as exc:
        validate_home_location(lat, lon)
    assert exc.value.status == 422
    assert exc.value.code == "INVALID_HOME_LOCATION"


def test_one_axis_at_zero_is_a_real_place():
    # The equator / prime meridian are places people live.
    assert validate_home_location(0, 32.58) == (0.0, 32.58)
    assert validate_home_location(51.4779, -0.00001) == (51.4779, 0.0)


def test_negative_zero_is_folded():
    lat, _lon = validate_home_location(-0.00001, 32.58)
    assert math.copysign(1.0, lat) == 1.0


def test_platform_error_is_a_409_with_a_stable_code():
    err = HomeLocationFromPlatformError()
    assert (err.status, err.code) == (409, "HOME_LOCATION_FROM_PLATFORM")


def test_home_location_is_frozen():
    home = HomeLocation(latitude=1.0, longitude=2.0, from_platform=False)
    with pytest.raises(AttributeError):
        home.latitude = 3.0  # type: ignore[misc]


# ─── coarse_home_for_peer: the home a paired household gets ──────────────


#: Sample homes: equator, mid-latitudes both hemispheres, the far north,
#: the date line on both sides, the poles' neighbourhood.
_SAMPLE_HOMES = [
    (0.0001, 0.0001),
    (47.376912, 8.541698),
    (52.520008, 13.404954),
    (-33.868820, 151.209290),
    (70.123456, 25.654321),
    (64.1466, -21.9426),
    (-54.8019, -68.3030),
    (35.6762, 179.9999),
    (-17.7134, -179.9999),
    (89.9999, 10.0),
    (-89.9999, -10.0),
    (84.5, 45.0),
    (1.2345, 180.0),
    (1.2345, -180.0),
]


@pytest.mark.parametrize(("lat", "lon"), _SAMPLE_HOMES)
def test_the_peer_home_is_within_2_5_km_of_the_real_one(lat, lon):
    clat, clon = coarse_home_for_peer(lat, lon)
    assert haversine_m(lat, lon, clat, clon) <= 2500


@pytest.mark.parametrize(("lat", "lon"), _SAMPLE_HOMES)
def test_a_cell_centre_snaps_to_itself(lat, lon):
    centre = coarse_home_for_peer(lat, lon)
    assert coarse_home_for_peer(*centre) == centre


@pytest.mark.parametrize(("lat", "lon"), _SAMPLE_HOMES)
def test_the_peer_home_is_a_valid_home(lat, lon):
    clat, clon = coarse_home_for_peer(lat, lon)
    assert validate_home_location(clat, clon) == (clat, clon)


def test_nearby_homes_on_the_same_street_get_the_same_point():
    # Two houses ~100 m apart, well inside one cell: the peer cannot tell
    # them apart, and repeated sends of a moving home leak nothing more.
    a = coarse_home_for_peer(47.3760, 8.5410)
    b = coarse_home_for_peer(47.3765, 8.5420)
    assert a == b
    assert a != (47.376, 8.541)


def test_it_is_coarse_not_the_street():
    lat, lon = 47.376912, 8.541698
    clat, clon = coarse_home_for_peer(lat, lon)
    # Never the 4 dp home (or anything close to it by accident of input).
    assert (clat, clon) != validate_home_location(lat, lon)
    # Latitude lands on the 0.03° grid centre (an odd multiple of 0.015).
    assert round((clat + 90) / PEER_HOME_GRID_DEG - 0.5, 6).is_integer()


def test_the_cell_is_about_3_km_on_both_axes():
    # The neighbouring cell's centre east and north are ~3 km away.
    lat, lon = coarse_home_for_peer(52.52, 13.40)
    north = coarse_home_for_peer(lat + PEER_HOME_GRID_DEG, lon)
    step = PEER_HOME_GRID_DEG / math.cos(math.radians(lat))
    east = coarse_home_for_peer(lat, lon + step)
    assert 3000 <= haversine_m(lat, lon, *north) <= 3600
    assert 3000 <= haversine_m(lat, lon, *east) <= 3600


def test_it_is_deterministic():
    assert coarse_home_for_peer(47.376912, 8.541698) == coarse_home_for_peer(
        47.376912, 8.541698
    )


def test_signs_follow_the_hemisphere_and_never_negative_zero():
    assert coarse_home_for_peer(-0.01, 8.5)[0] < 0  # a southern centre
    assert coarse_home_for_peer(47.3, -0.5)[1] < 0  # a western centre
    # Near the equator / prime meridian a centre can sit on 0: never -0.0.
    for point in ((-0.01, -0.01), (0.0001, 0.0001)):
        for value in coarse_home_for_peer(*point):
            assert value != 0.0 or math.copysign(1.0, value) == 1.0


@pytest.mark.parametrize(
    ("lat", "lon"),
    [(None, 8.5), (47.3, "x"), (91.0, 8.5), (47.3, 181.0), (0, 0), (True, 1)],
)
def test_garbage_is_refused(lat, lon):
    with pytest.raises(InvalidHomeLocationError):
        coarse_home_for_peer(lat, lon)


@pytest.mark.parametrize(
    ("lat", "lon"),
    [(None, None), (47.3, None), (None, 8.5), ("x", 8.5), (95.0, 8.5), (0, 0)],
)
def test_or_none_gives_none_for_a_missing_or_bad_home(lat, lon):
    assert coarse_home_for_peer_or_none(lat, lon) is None


def test_or_none_coarsens_a_good_home():
    assert coarse_home_for_peer_or_none(47.376912, 8.541698) == (
        coarse_home_for_peer(47.376912, 8.541698)
    )
