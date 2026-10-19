"""The household home location's pure rules."""

from __future__ import annotations

import math

import pytest

from socialhome.domain.home_location import (
    HomeLocation,
    HomeLocationFromPlatformError,
    InvalidHomeLocationError,
    validate_home_location,
)


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
