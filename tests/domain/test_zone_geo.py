"""Tests for :mod:`socialhome.domain.zone_geo` — the one haversine
nearest-zone-within-radius helper shared by space zones and household
zones."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from socialhome.domain.zone_geo import EARTH_RADIUS_M, haversine_m, match_zone


@dataclass(frozen=True)
class _Zone:
    name: str
    latitude: float
    longitude: float
    radius_m: int


def test_haversine_zero_for_same_point():
    assert haversine_m(52.52, 13.405, 52.52, 13.405) == 0.0


def test_haversine_one_degree_latitude_is_about_111_km():
    d = haversine_m(0.0, 0.0, 1.0, 0.0)
    assert d == pytest.approx(EARTH_RADIUS_M * 3.141592653589793 / 180, rel=1e-9)
    assert 111_000 < d < 111_400


def test_haversine_is_symmetric():
    a = haversine_m(48.1371, 11.5754, 52.5200, 13.4050)
    b = haversine_m(52.5200, 13.4050, 48.1371, 11.5754)
    assert a == pytest.approx(b)
    # Munich → Berlin is roughly 504 km.
    assert 500_000 < a < 510_000


def test_match_zone_none_for_empty_list():
    assert match_zone([], 1.0, 2.0) is None


def test_match_zone_inside_radius():
    z = _Zone("Home", 52.5200, 13.4050, 100)
    assert match_zone([z], 52.5201, 13.4050) is z


def test_match_zone_outside_radius():
    z = _Zone("Home", 52.5200, 13.4050, 100)
    # ~1.1 km north.
    assert match_zone([z], 52.5300, 13.4050) is None


def test_match_zone_boundary_is_inclusive():
    z = _Zone("Edge", 0.0, 0.0, 0)
    assert match_zone([z], 0.0, 0.0) is z


def test_match_zone_picks_nearest_of_overlapping():
    big = _Zone("City", 52.5200, 13.4050, 50_000)
    small = _Zone("Office", 52.5210, 13.4060, 200)
    assert match_zone([big, small], 52.5211, 13.4061) is small
    assert match_zone([small, big], 52.5211, 13.4061) is small


def test_match_zone_falls_back_to_larger_zone():
    big = _Zone("City", 52.5200, 13.4050, 50_000)
    small = _Zone("Office", 52.5210, 13.4060, 200)
    assert match_zone([small, big], 52.60, 13.40) is big
