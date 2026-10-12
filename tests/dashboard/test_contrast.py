"""Tests for the dashboard's colour validation + WCAG contrast helpers."""

from __future__ import annotations

import re

import pytest

from socialhome.dashboard.contrast import (
    AA_TEXT,
    WARM_INK,
    contrast_ratio,
    hex_color,
    ink_on,
    note_ink,
    readable_on,
    relative_luminance,
)
from socialhome.dashboard.render import STATIC_DIR


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("#AbCdEf", "#abcdef"),
        ("#abc", "#aabbcc"),
        ("  #123456 ", "#123456"),
    ],
)
def test_hex_color_normalises(raw, expected):
    assert hex_color(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "red",
        "#12345",
        "#1234567",
        "#abcd",  # alpha shapes are refused: contrast can't be judged
        "#aabbccdd",
        "#ggg",
        "#123456;}body{display:none",
        "url(x)",
        "#12345\n6",
        42,
    ],
)
def test_hex_color_refuses_anything_else(raw):
    assert hex_color(raw) is None


def test_relative_luminance_extremes():
    assert relative_luminance("#000000") == 0.0
    assert relative_luminance("#ffffff") == pytest.approx(1.0)


def test_contrast_ratio_black_on_white_is_21():
    assert contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast_ratio("#ffffff", "#000000") == pytest.approx(21.0)
    assert contrast_ratio("#777777", "#777777") == pytest.approx(1.0)


def test_contrast_ratio_known_value():
    # WCAG reference: #767676 on white is just above 4.5:1.
    assert contrast_ratio("#767676", "#ffffff") == pytest.approx(4.54, abs=0.01)
    assert contrast_ratio("#777777", "#ffffff") < AA_TEXT


@pytest.mark.parametrize(
    ("bg", "ink"),
    [
        ("#ffffff", "#000000"),
        ("#ffee88", "#000000"),
        ("#000000", "#ffffff"),
        ("#2f6f5e", "#ffffff"),
        ("#777777", "#000000"),  # mid grey: black wins (4.69 vs 4.48)
    ],
)
def test_ink_on_picks_the_higher_contrast(bg, ink):
    assert ink_on(bg) == ink
    assert contrast_ratio(ink_on(bg), bg) >= AA_TEXT


@pytest.mark.parametrize(
    ("bg", "ink"),
    [
        ("#fff9b1", WARM_INK),  # a pastel note: the board's warm ink
        ("#ffee88", WARM_INK),
        ("#2f6f5e", "#ffffff"),  # white ink stays white
        ("#777777", "#000000"),  # black only just reads: no softer ink
    ],
)
def test_note_ink_is_warm_where_it_still_reads(bg, ink):
    assert note_ink(bg) == ink
    assert contrast_ratio(note_ink(bg), bg) >= AA_TEXT


def test_warm_ink_is_the_board_text_colour():
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    assert f"--dash-text: {WARM_INK};" in css


def test_readable_on_needs_every_surface():
    assert readable_on("#2f6f5e", ("#ffffff", "#f4f2ee")) is True
    assert readable_on("#d2542a", ("#ffffff",)) is False  # 4.06:1
    assert readable_on("#ffee88", ("#ffffff", "#151412")) is False
    assert readable_on("#ffee88", ("#151412", "#22201d")) is True


def _rule(css: str, selector: str) -> str:
    match = re.search(rf"(?m)^{re.escape(selector)} \{{([^}}]*)\}}", css)
    assert match is not None, selector
    return match.group(1)


def _token(block: str, name: str) -> str:
    found = re.search(rf"--dash-{name}: (#[0-9a-f]{{6}});", block)
    assert found is not None, name
    return found.group(1)


def test_picture_view_captions_are_readable_on_both_themes():
    """The picture view's helper captions and section labels are small
    print in the muted ink: it must still reach AA (4.5:1) on the tile
    and card surfaces, light and dark — and nothing may fade it."""
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    light = css[css.index(":root {") :]
    dark = css[css.index("@media (prefers-color-scheme: dark)") :]
    for block in (light, dark):
        surfaces = (_token(block, "bg"), _token(block, "surface"))
        assert readable_on(_token(block, "muted"), surfaces)
    # Night: the caption under tomorrow's first thing sits on the page.
    night = css[css.index(".dash-night {") :]
    assert readable_on(_token(night, "muted"), (_token(night, "bg"),))
    for selector in (".dash-pv-cap", ".dash-pv-sec"):
        body = _rule(css, selector)
        assert "color: var(--dash-muted);" in body, selector
        assert "opacity" not in body, selector
