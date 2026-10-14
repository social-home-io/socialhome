"""Tests for the wall map's framing (:mod:`socialhome.dashboard.map_frame`).

The shared cases in ``map_frame_vectors.json`` are what ``map.js`` (the
browser Map tab) picks — ``client/src/dashboardMapScript.test.ts`` checks
the script against the very same file, so the panel picture and the
browser frame a household alike.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from socialhome.dashboard.map_frame import (
    MAX_ZOOM,
    MIN_FIT_ZOOM,
    TILE,
    Fit,
    Frame,
    best_zoom,
    edge_point,
    ideal_frame,
    insets,
    js_round,
    meters_per_px,
    project,
    tile_grid,
)

VECTORS = json.loads(
    (Path(__file__).parent / "map_frame_vectors.json").read_text(encoding="utf-8")
)["cases"]


def _fits(case: dict) -> list[Fit]:
    return [
        Fit(
            lat=g["lat"],
            lon=g["lon"],
            r=g["r"],
            home=g["home"],
            members=len(g["members"]),
        )
        for g in case["groups"]
    ]


@pytest.mark.parametrize("case", VECTORS, ids=[c["name"] for c in VECTORS])
def test_frames_like_map_js(case):
    fits = _fits(case)
    framing = ideal_frame(fits, case["w"], case["h"])
    x0, y0 = framing.frame.origin(case["w"], case["h"])
    expect = case["expect"]
    assert framing.frame.z == expect["z"]
    assert (x0, y0) == (expect["x0"], expect["y0"])
    far = sorted(case["groups"][i]["key"] for i in framing.far)
    assert far == expect["far"]
    assert len(tile_grid(framing.frame, case["w"], case["h"])) == expect["tiles"]


def test_project_and_scale():
    x, y = project(0, 0, 0)
    assert (x, y) == (128, 128)
    x, y = project(85.1, 180, 1)
    assert x == 512 and -1 < y < 1
    # Clamped like map.js: the poles never reach infinity.
    assert math.isfinite(project(90, 0, 3)[1])
    assert meters_per_px(0, 0) == pytest.approx(156543.03392)
    assert meters_per_px(60, 1) == pytest.approx(156543.03392 / 4, rel=1e-9)


def test_js_round_rounds_half_up():
    assert js_round(2.5) == 3
    assert js_round(-2.5) == -2
    assert js_round(1.49) == 1


def test_insets_shrink_on_narrow_pictures():
    assert insets(1000) == (140, 60, 140)
    assert insets(400) == (140, 60, 80)


def test_best_zoom_is_never_closer_than_16_and_minus_one_when_nothing_fits():
    one = [Fit(lat=52.52, lon=13.405, r=0, home=False, members=1)]
    assert best_zoom(one, 748, 560, MIN_FIT_ZOOM) == MAX_ZOOM
    apart = [
        Fit(lat=52.52, lon=13.405, r=0, home=True, members=1),
        Fit(lat=-33.86, lon=151.2, r=0, home=False, members=1),
    ]
    assert best_zoom(apart, 748, 560, MIN_FIT_ZOOM) == -1
    assert best_zoom(apart, 748, 560, 0) >= 0


def test_frame_centres_people_in_the_free_region():
    fit = Fit(lat=52.52, lon=13.405, r=0, home=True, members=1)
    framing = ideal_frame([fit], 748, 560)
    x, y = framing.frame.to_box(fit.lat, fit.lon, 748, 560)
    top, bottom, _ = insets(748)
    assert x == pytest.approx(374)
    # The middle of the band between the bubble room above and the names below.
    assert y == pytest.approx(top + (560 - top - bottom) / 2)


def test_tile_grid_covers_the_box_and_wraps_columns():
    frame = Frame(z=2, cx=10.0, cy=512.0)  # straddles the antimeridian
    grid = tile_grid(frame, 300, 256)
    xs = {t.x for t in grid}
    assert xs <= {0, 1, 2, 3}
    assert {t.x for t in grid if t.left < 0} == {3}  # wrapped from column -1
    for t in grid:
        assert t.z == 2 and 0 <= t.y < 4
        assert t.left % 1 == 0 and t.top % 1 == 0
    covered = {
        (t.left + dx, t.top + dy)
        for t in grid
        for dx in (0, TILE - 1)
        for dy in (0, TILE - 1)
    }
    assert any(px <= 0 for px, _ in covered) and any(px >= 299 for px, _ in covered)


def test_tile_grid_skips_rows_off_the_world():
    frame = Frame(z=0, cx=128.0, cy=0.0)
    grid = tile_grid(frame, 256, 256)
    assert {(t.x, t.y) for t in grid} == {(0, 0)}


def test_edge_point_heads_toward_the_far_person():
    # Far to the east, a little south of the centre: the right edge.
    x, y, angle, along_y = edge_point(2400.0, 500.0, 800, 600, 200, 48)
    assert along_y is True
    assert x == pytest.approx(800 - 100 - 10)
    dx, dy = 2400 - 400, 500 - 300
    assert y == pytest.approx(300 + dy * (290 / dx))
    assert angle == pytest.approx(math.degrees(math.atan2(dy, dx)))
    # Straight north: the top edge, sliding sideways.
    x, y, angle, along_y = edge_point(400.0, -5000.0, 800, 600, 200, 48)
    assert along_y is False
    assert (x, y) == (pytest.approx(400), pytest.approx(24 + 10))
    assert angle == pytest.approx(-90)
