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
    directory_verdict,
    edge_band_km,
    edge_cell,
    edge_max_shift_km,
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
    assert wire == {
        "local_community": True,
        "radius_km": 5.0,
        "in_area": in_area,
        "area_edge": False,
    }
    assert not {"lat", "lon", "latitude", "longitude", "local_area"} & set(wire)


@pytest.mark.parametrize("in_area", [True, False, None])
def test_directory_shape_round_trip(in_area):
    wire = {"local_community": True, "radius_km": 2.0, "in_area": in_area}
    assert parse_local_community_wire(wire) == PeerLocalCommunity(2.0, in_area)


def test_near_the_edge_is_unsure_on_the_wire():
    """Near the rim the host says neither yes nor no: ``in_area: null``
    with ``area_edge: true`` — a receiver that does not know ``area_edge``
    reads it as unknown, never as a wrong yes or no."""
    wire = local_community_wire(LocalArea(47.3769, 8.5417, 5.0), Eligibility.EDGE)
    assert wire == {
        "local_community": True,
        "radius_km": 5.0,
        "in_area": None,
        "area_edge": True,
    }
    assert parse_local_community_wire(wire) == PeerLocalCommunity(
        5.0, None, area_edge=True
    )


@pytest.mark.parametrize("in_area", [True, False])
def test_an_edge_flag_beside_a_verdict_is_ignored(in_area):
    """Unambiguous: ``area_edge`` only ever stands beside ``in_area:
    null``; a sender that says both gets its yes / no read, not the flag."""
    got = parse_local_community_wire(
        {"local_community": True, "radius_km": 5, "in_area": in_area, "area_edge": True}
    )
    assert got == PeerLocalCommunity(5.0, in_area, area_edge=False)


@pytest.mark.parametrize("bad", ["yes", 1, None, []])
def test_a_non_boolean_edge_flag_is_no_edge(bad):
    got = parse_local_community_wire(
        {"local_community": True, "radius_km": 5, "in_area": None, "area_edge": bad}
    )
    assert got == PeerLocalCommunity(5.0, None, area_edge=False)


# ── the "unsure" band of the directory verdict ──────────────────────────


@pytest.mark.parametrize(
    "radius,band", [(0.5, 0.25), (2.0, 0.25), (2.5, 0.25), (5.0, 0.5), (50.0, 5.0)]
)
def test_the_band_is_a_tenth_of_the_radius_and_at_least_250_m(radius, band):
    assert edge_band_km(radius) == pytest.approx(band)


#: Kilometres per degree on the haversine sphere.
_KM_PER_DEG = 111.19492664455873


def _east_km(km: float, north_km: float = 0.0) -> tuple[float, float]:
    """A point ``km`` east (and ``north_km`` north) of (0, 0) on the equator."""
    return (north_km / _KM_PER_DEG, km / _KM_PER_DEG)


@pytest.mark.parametrize(
    "km,verdict",
    [
        (0.0, Eligibility.INSIDE),
        (4.30, Eligibility.INSIDE),
        (4.80, Eligibility.EDGE),
        (5.00, Eligibility.EDGE),
        (5.20, Eligibility.EDGE),
        (5.70, Eligibility.OUTSIDE),
        (9.00, Eligibility.OUTSIDE),
    ],
)
def test_near_the_rim_the_directory_verdict_is_unsure(km, verdict):
    area = LocalArea(0.0, 0.0, 5.0)  # band 0.5 km: cells 0.5 km, unsure ±0.625
    assert directory_verdict(area, _east_km(km), shift_km=0.0) is verdict


def test_the_directory_verdict_never_says_a_wrong_yes_or_no():
    """For any home and any shift the host may draw, "inside" is truly
    inside and "outside" truly outside — the coarsening only ever adds
    "unsure"."""
    for radius in (0.5, 2.0, 5.0, 25.0):
        area = LocalArea(0.0, 0.0, radius)
        band = edge_band_km(radius)
        for i in range(-80, 81):
            for j in (-0.37, 0.0, 0.11, 0.49):
                km = radius + i * band / 20
                home = _east_km(km, north_km=j * band)
                exact = eligibility(area, home)
                for shift in (-band, -band / 2, 0.0, band / 8, band / 2, band):
                    got = directory_verdict(area, home, shift_km=shift)
                    if got is not Eligibility.EDGE:
                        assert got is exact, (radius, km, j, shift)


def test_every_home_in_a_cell_gets_the_same_answer():
    """The verdict is the cell's, not the home's: stepping a home within a
    cell learns nothing."""
    area = LocalArea(0.0, 0.0, 5.0)
    cell = edge_band_km(5.0)
    for i in range(30):
        start = 4.0 + i * cell
        answers = {
            directory_verdict(area, _east_km(start + f * cell), shift_km=0.1)
            for f in (0.01, 0.3, 0.6, 0.99)
        }
        assert len(answers) == 1


def test_a_shift_beyond_its_bound_is_clamped():
    """The shift is the host's secret, but it can never slide the band so
    far that a wrong yes or no becomes possible."""
    area = LocalArea(0.0, 0.0, 5.0)
    huge = directory_verdict(area, _east_km(4.6), shift_km=99)
    capped = directory_verdict(area, _east_km(4.6), shift_km=edge_max_shift_km(5.0))
    assert huge is capped


def test_an_unknown_home_stays_unknown_in_the_directory():
    area = LocalArea(0.0, 0.0, 5.0)
    assert directory_verdict(area, None, shift_km=0.0) is Eligibility.HOME_UNKNOWN


def test_the_seat_check_itself_keeps_the_exact_distance():
    """``eligibility`` (what every seat path runs) has no band and no
    grid: just inside is inside, just outside is outside."""
    area = LocalArea(0.0, 0.0, 5.0)
    assert eligibility(area, _east_km(4.99)) is Eligibility.INSIDE
    assert eligibility(area, _east_km(5.01)) is Eligibility.OUTSIDE


def test_the_grid_is_absolute_and_a_band_wide():
    """Cells never depend on the centre (the grid would point at it)."""
    near = LocalArea(0.0, 0.0, 5.0)
    moved = LocalArea(0.0123, 0.0456, 5.0)
    home = _east_km(4.80)
    assert edge_cell(near, home) == edge_cell(moved, home)
    assert edge_cell(near, _east_km(4.80)) == edge_cell(near, _east_km(4.81))
    assert edge_cell(near, _east_km(4.80)) != edge_cell(near, _east_km(5.10))


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
