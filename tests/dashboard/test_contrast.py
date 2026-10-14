"""Tests for the dashboard's colour validation + WCAG contrast helpers."""

from __future__ import annotations

import re

import pytest

from socialhome.dashboard.contrast import (
    AA_TEXT,
    BLACK,
    BUBBLE_DARK,
    BUBBLE_LIGHT,
    WHITE,
    frame_color,
    mix_srgb,
    NOTE_DARK_BG,
    NOTE_LIGHT_BG,
    NOTE_MIX_DARK,
    NOTE_MIX_LIGHT,
    PAST_FADE,
    SURFACE_DARK,
    SURFACE_LIGHT,
    SURFACE_NIGHT,
    WARM_INK,
    mix_oklab,
    note_colors,
    contrast_ratio,
    hex_color,
    ink_on,
    note_ink,
    past_colors,
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


def test_mix_oklab_known_values():
    assert mix_oklab("#8855ee", "#8855ee", 0.82) == "#8855ee"
    assert mix_oklab("#ffffff", "#000000", 1.0) == "#ffffff"
    assert mix_oklab("#ffffff", "#000000", 0.0) == "#000000"
    # Oklab L 0.5 grey: linear 0.125 → sRGB 99.
    assert mix_oklab("#ffffff", "#000000", 0.5) == "#636363"


def test_note_colours_are_the_softened_fills_with_their_ink():
    note = note_colors("#8855ee")
    assert note.light_bg == mix_oklab("#8855ee", NOTE_LIGHT_BG, NOTE_MIX_LIGHT)
    assert note.dark_bg == mix_oklab("#8855ee", NOTE_DARK_BG, NOTE_MIX_DARK)
    # The two the review proved broken against the raw colour.
    assert contrast_ratio(note.light_ink, note.light_bg) >= AA_TEXT
    dark = note_colors("#5588bb")
    assert contrast_ratio(dark.dark_ink, dark.dark_bg) >= AA_TEXT
    # A pastel keeps the warm ink.
    assert note_colors("#fff9b1").light_ink == WARM_INK


_STEPS = range(0, 256, 51)
_GRID = [f"#{r:02x}{g:02x}{b:02x}" for r in _STEPS for g in _STEPS for b in _STEPS]


@pytest.mark.parametrize("color", [*_GRID, "#5588bb", "#8855ee", "#777777"])
def test_note_ink_reads_on_the_softened_fill_in_both_modes(color):
    note = note_colors(color)
    assert contrast_ratio(note.light_ink, note.light_bg) >= AA_TEXT, color
    assert contrast_ratio(note.dark_ink, note.dark_bg) >= AA_TEXT, color


def test_note_mix_grounds_are_the_page_backgrounds():
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    assert f"--dash-bg: {NOTE_LIGHT_BG};" in css
    assert f"--dash-bg: {NOTE_DARK_BG};" in css
    # The CSS no longer mixes note colours itself.
    assert "color-mix(in oklab, var(--kc" not in css


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


# ─── Map marker frames (--kf) ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("color", "light", "dark"),
    [
        # The approved design's table (round 2).
        ("#3e63dd", "#3e63dd", "#6582e4"),
        ("#1f4a3a", "#1f4a3a", "#799289"),
        ("#c8922e", "#be8b2c", "#c8922e"),
        ("#d2552f", "#d2552f", "#d66644"),
        ("#12a594", "#12a594", "#12a594"),
        ("#8e4ec6", "#8e4ec6", "#a571d1"),
    ],
)
def test_frame_colors_match_the_design(color, light, dark):
    assert frame_color(color, BUBBLE_LIGHT, BLACK) == light
    assert frame_color(color, BUBBLE_DARK, WHITE) == dark


def _grid() -> list[str]:
    steps = ("00", "33", "66", "99", "cc", "ff")
    return [f"#{r}{g}{b}" for r in steps for g in steps for b in steps]


@pytest.mark.parametrize("color", _grid())
def test_frame_color_clears_non_text_contrast_on_both_bubbles(color):
    """WCAG 1.4.11: a person's marker frame reads (3:1) on the bubble —
    white in the light theme, the lifted dark bubble in the dark one."""
    assert contrast_ratio(frame_color(color, BUBBLE_LIGHT, BLACK), BUBBLE_LIGHT) >= 3
    assert contrast_ratio(frame_color(color, BUBBLE_DARK, WHITE), BUBBLE_DARK) >= 3


def test_mix_srgb():
    assert mix_srgb("#000000", "#ffffff", 0.5) == "#808080"
    assert mix_srgb("#123456", "#000000", 0.0) == "#123456"
    assert mix_srgb("#123456", "#ffffff", 1.0) == "#ffffff"


# ─── Past timeline bars (--kp / --kq) ────────────────────────────────────


def _palette(css: str, start: str) -> dict[str, str]:
    at = css.index(start)
    block = css[at : css.index("}", at)]
    return dict(re.findall(r"(--dash-[a-z-]+):\s*(#[0-9a-f]{6});", block))


def _palettes() -> dict[str, dict[str, str]]:
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    light = _palette(css, ":root {")
    dark = {**light, **_palette(css, "@media (prefers-color-scheme: dark)")}
    night = {**dark, **_palette(css, ".dash-night {")}
    return {"light": light, "dark": dark, "night": night}


def test_past_surfaces_are_the_card_surfaces():
    palettes = _palettes()
    assert palettes["light"]["--dash-surface"] == SURFACE_LIGHT
    assert palettes["dark"]["--dash-surface"] == SURFACE_DARK
    assert palettes["night"]["--dash-surface"] == SURFACE_NIGHT


def test_past_colors_fade_only_the_fill():
    """The fill is what ``opacity: 0.5`` drew over the card; the ink is
    picked against it (the panel firmware's rule)."""
    past = past_colors("#ffee88")
    assert PAST_FADE == 0.5
    assert past.light_bg == mix_srgb("#ffee88", SURFACE_LIGHT, 0.5) == "#fff6c4"
    assert past.dark_bg == mix_srgb("#ffee88", SURFACE_DARK, 0.5)
    assert past.night_bg == mix_srgb("#ffee88", SURFACE_NIGHT, 0.5)
    for bg, ink in (
        (past.light_bg, past.light_ink),
        (past.dark_bg, past.dark_ink),
        (past.night_bg, past.night_ink),
    ):
        assert ink == ink_on(bg)


def test_dark_past_bar_label_was_unreadable_and_now_reads():
    """Regression: a red bar's black label at 50 % opacity on the dark
    card was ~2.2:1; the full-strength ink on the faded fill clears AA."""
    color = "#e53935"
    faded_ink = mix_srgb(ink_on(color), SURFACE_DARK, 0.5)
    faded_fill = mix_srgb(color, SURFACE_DARK, 0.5)
    assert contrast_ratio(faded_ink, faded_fill) < 2.5
    past = past_colors(color)
    assert contrast_ratio(past.dark_ink, past.dark_bg) >= AA_TEXT


@pytest.mark.parametrize(
    "color",
    ["#ffee88", "#112233", "#8855ee", "#5588bb", "#e53935", "#43a047", "#808080"],
)
def test_past_bar_ink_is_the_better_of_black_and_white(color):
    past = past_colors(color)
    for bg, ink in (
        (past.light_bg, past.light_ink),
        (past.dark_bg, past.dark_ink),
        (past.night_bg, past.night_ink),
    ):
        ratio = contrast_ratio(ink, bg)
        assert ratio >= max(contrast_ratio(BLACK, bg), contrast_ratio(WHITE, bg))
        assert ratio >= AA_TEXT, (color, bg, ink)


def test_past_fallback_matches_past_colors_of_muted():
    """A bar with no colour of its own fades --dash-muted: the CSS
    fallback (--dash-past-fill / --dash-past-ink) per scheme is what
    :func:`past_colors` gives for it on that scheme's card."""
    for name, palette in _palettes().items():
        past = past_colors(palette["--dash-muted"])
        bg, ink = {
            "light": (past.light_bg, past.light_ink),
            "dark": (past.dark_bg, past.dark_ink),
            "night": (past.night_bg, past.night_ink),
        }[name]
        assert palette["--dash-past-fill"] == bg, name
        assert palette["--dash-past-ink"] == ink, name
        assert contrast_ratio(ink, bg) >= AA_TEXT, name


def test_past_bars_no_longer_fade_their_label():
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    rule = css[css.index(".dash-block.dash-past {") :]
    rule = rule[: rule.index("}")]
    assert "opacity: 1;" in rule
    assert "color: var(--kq, var(--dash-past-ink));" in rule
    assert "background-color: var(--kp, var(--dash-past-fill));" in rule
    emoji = css[css.index(".dash-block.dash-past .dash-block-icon,") :]
    assert emoji[: emoji.index("}")].endswith("opacity: 0.5;\n")
