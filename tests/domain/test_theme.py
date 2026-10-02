"""Tests for theme field validation (domain/theme.py)."""

from __future__ import annotations

import pytest

from socialhome.domain.theme import (
    ALLOWED_FONTS,
    ALLOWED_POST_LAYOUTS,
    SpaceTheme,
    ThemeValidationError,
    validate_choice,
    validate_color,
    validate_corner_radius,
    validate_optional_choice,
    validate_optional_color,
)


def test_allowed_sets_match_the_schema_check():
    # Migration 0001 CHECK(font_family IN (...)) / CHECK(post_layout IN (...)).
    assert ALLOWED_FONTS == {"system", "serif", "rounded", "mono"}
    assert ALLOWED_POST_LAYOUTS == {"card", "compact", "magazine"}


def test_validation_error_is_a_value_error():
    assert issubclass(ThemeValidationError, ValueError)


def test_validate_color_normalises_and_names_field():
    assert validate_color("#ABCDEF", "primary_color") == "#abcdef"
    with pytest.raises(ThemeValidationError) as exc:
        validate_color("<b>red</b>", "primary_color")
    assert "primary_color" in str(exc.value)
    assert "<b>" not in str(exc.value)


def test_validate_optional_color_passes_none():
    assert validate_optional_color(None, "background_tint") is None
    with pytest.raises(ThemeValidationError, match="background_tint"):
        validate_optional_color("#fff", "background_tint")


def test_validate_choice_lists_allowed_values_not_input():
    with pytest.raises(ThemeValidationError) as exc:
        validate_choice("Inter, sans-serif", ALLOWED_FONTS, "font_family")
    msg = str(exc.value)
    assert msg == "font_family must be one of: mono, rounded, serif, system"


def test_validate_choice_rejects_non_string():
    with pytest.raises(ThemeValidationError):
        validate_choice(["serif"], ALLOWED_FONTS, "font_family")


def test_validate_optional_choice():
    assert validate_optional_choice(None, ALLOWED_FONTS, "x") is None
    assert validate_optional_choice("mono", ALLOWED_FONTS, "x") == "mono"


@pytest.mark.parametrize("value", [0, 24, "12"])
def test_validate_corner_radius_accepts(value):
    assert validate_corner_radius(value) == int(value)


@pytest.mark.parametrize(
    "value",
    [
        -1,
        25,
        "abc",
        None,
        True,
        1.5,
        12.0,
        "-1",
        " 12",
        "12 ",
        "+3",
        "１２",
        "١٢",
        "1_2",
        "0x1",
    ],
)
def test_validate_corner_radius_rejects(value):
    with pytest.raises(ThemeValidationError, match="corner_radius"):
        validate_corner_radius(value)


def test_space_theme_defaults():
    t = SpaceTheme(space_id="s")
    assert (t.font_family, t.post_layout) == ("system", "card")


def test_validate_color_rejects_trailing_newline():
    # ``re.match`` + ``$`` would let "#aabbcc\n" through.
    with pytest.raises(ThemeValidationError):
        validate_color("#aabbcc\n", "primary_color")
