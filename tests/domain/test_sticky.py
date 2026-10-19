"""Tests for socialhome.domain.sticky — field rules shared by every path."""

from __future__ import annotations

import pytest

from socialhome.domain.sticky import (
    DEFAULT_STICKY_COLOR,
    MAX_STICKY_CONTENT_LENGTH,
    STICKY_BOARD_HEIGHT,
    STICKY_BOARD_WIDTH,
    STICKY_NOTE_HEIGHT,
    STICKY_NOTE_WIDTH,
    coerce_peer_sticky,
    free_sticky_spot,
    normalize_sticky_color,
    parse_sticky_coord,
    sanitize_sticky_content,
)


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("#FFF9B1", "#FFF9B1"),
        ("#fff9b1", "#FFF9B1"),
        ("#abc", "#AABBCC"),
        ("#ABC", "#AABBCC"),
    ],
)
def test_normalize_sticky_color_accepts_hex(raw, want):
    assert normalize_sticky_color(raw) == want


@pytest.mark.parametrize(
    "raw",
    [
        "url(https://evil.example/t.png)",
        "red",
        "yellow",
        "#FFF9B1; background-image: url(x)",
        "red;color:blue",
        "#12345",
        "#1234567",
        "FFF9B1",
        "#GGGGGG",
        "#FFF\n",
        "x" * 5000,
        "",
        None,
        123,
        ["#FFF"],
    ],
)
def test_normalize_sticky_color_refuses_everything_else(raw):
    assert normalize_sticky_color(raw) is None


def test_default_color_is_canonical():
    assert normalize_sticky_color(DEFAULT_STICKY_COLOR) == DEFAULT_STICKY_COLOR


def test_parse_sticky_coord_clamps_into_board():
    assert parse_sticky_coord(12.5, limit=STICKY_BOARD_WIDTH) == 12.5
    assert parse_sticky_coord(7, limit=STICKY_BOARD_WIDTH) == 7.0
    assert parse_sticky_coord(-5, limit=STICKY_BOARD_WIDTH) == 0.0
    assert parse_sticky_coord(1e9, limit=STICKY_BOARD_WIDTH) == STICKY_BOARD_WIDTH
    assert parse_sticky_coord(701, limit=STICKY_BOARD_HEIGHT) == STICKY_BOARD_HEIGHT


@pytest.mark.parametrize(
    "raw", [10**400, float("nan"), float("inf"), "12", True, None, {}, []]
)
def test_parse_sticky_coord_refuses_non_finite_or_non_numbers(raw):
    assert parse_sticky_coord(raw, limit=STICKY_BOARD_WIDTH) is None


def test_sanitize_sticky_content_strips_control_and_bidi_keeps_newlines():
    raw = "  buy\u202e milk\x00\n\ttomorrow\u200f  "
    assert sanitize_sticky_content(raw) == "buy milk\n\ttomorrow"
    assert sanitize_sticky_content("\u200b \x07") == ""


def test_content_cap_leaves_headroom_over_the_spa_limit():
    assert MAX_STICKY_CONTENT_LENGTH >= 500


def test_coerce_peer_sticky_replaces_bad_values_never_keeps_raw():
    f = coerce_peer_sticky(
        content="hi\u202e there\x00",
        color="url(https://evil.example/t.png)",
        position_x=10**400,
        position_y="12",
    )
    assert f.content == "hi there"
    assert f.color == DEFAULT_STICKY_COLOR
    assert (f.position_x, f.position_y) == (0.0, 0.0)
    assert f.truncated is False


def test_coerce_peer_sticky_keeps_valid_values_canonical_and_clamped():
    f = coerce_peer_sticky(
        content="note", color="#abc", position_x=5000.0, position_y=12.5
    )
    assert (f.content, f.color, f.position_x, f.position_y) == (
        "note",
        "#AABBCC",
        STICKY_BOARD_WIDTH,
        12.5,
    )


def test_coerce_peer_sticky_truncates_long_content():
    f = coerce_peer_sticky(
        content="y" * (MAX_STICKY_CONTENT_LENGTH + 50),
        color=None,
        position_x=None,
        position_y=None,
    )
    assert f.content == "y" * MAX_STICKY_CONTENT_LENGTH
    assert f.truncated is True


def test_coerce_peer_sticky_non_string_content_is_empty():
    f = coerce_peer_sticky(content=5, color=None, position_x=0, position_y=0)
    assert f.content == ""


def _overlaps(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return (
        abs(a[0] - b[0]) < STICKY_NOTE_WIDTH and abs(a[1] - b[1]) < STICKY_NOTE_HEIGHT
    )


def test_free_sticky_spot_on_an_empty_board_is_the_first_grid_cell():
    x, y = free_sticky_spot([])
    assert 0 <= x <= STICKY_BOARD_WIDTH - STICKY_NOTE_WIDTH
    assert 0 <= y <= STICKY_BOARD_HEIGHT - STICKY_NOTE_HEIGHT


def test_free_sticky_spot_never_overlaps_while_the_board_has_room():
    taken: list[tuple[float, float]] = []
    for _ in range(12):
        spot = free_sticky_spot(taken)
        assert not any(_overlaps(spot, t) for t in taken), (spot, taken)
        assert 0 <= spot[0] <= STICKY_BOARD_WIDTH - STICKY_NOTE_WIDTH
        assert 0 <= spot[1] <= STICKY_BOARD_HEIGHT - STICKY_NOTE_HEIGHT
        taken.append(spot)


def test_free_sticky_spot_skips_cells_a_hand_placed_note_covers():
    first = free_sticky_spot([])
    nudged = (first[0] + 30, first[1] + 10)  # a note dragged near the corner
    spot = free_sticky_spot([nudged])
    assert not _overlaps(spot, nudged)


def test_free_sticky_spot_on_a_full_board_still_stays_inside_and_spreads():
    taken = [(x, y) for x in range(0, 1000, 50) for y in range(0, 700, 50)]
    a = free_sticky_spot(taken)
    b = free_sticky_spot([*taken, a])
    for spot in (a, b):
        assert 0 <= spot[0] <= STICKY_BOARD_WIDTH - STICKY_NOTE_WIDTH
        assert 0 <= spot[1] <= STICKY_BOARD_HEIGHT - STICKY_NOTE_HEIGHT
    assert a != b
