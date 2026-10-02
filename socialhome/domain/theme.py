"""Theme domain types + field validation (§23.123, §23.125).

The allowed choices mirror the ``household_theme`` / ``space_themes``
CHECK constraints in migration 0001 — the schema is the on-disk
authority, these sets are the in-code one. A theme stores *ids*
(``"serif"``, ``"magazine"``), never CSS values: the SPA maps an id to
a font stack / layout at paint time (``client/src/utils/themeFonts.ts``,
``client/src/utils/themeLayouts.ts``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ALLOWED_MODES = frozenset({"light", "dark", "auto"})
ALLOWED_FONTS = frozenset({"system", "serif", "rounded", "mono"})
ALLOWED_DENSITIES = frozenset({"compact", "comfortable", "spacious"})
ALLOWED_POST_LAYOUTS = frozenset({"card", "compact", "magazine"})

#: Column defaults for the two non-null space override fields — what a
#: ``null`` in a space-theme patch resets them to.
DEFAULT_FONT = "system"
DEFAULT_POST_LAYOUT = "card"

_HEX_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")
_ASCII_DIGITS_RE = re.compile(r"[0-9]{1,3}")


class ThemeValidationError(ValueError):
    """A theme field holds a value the schema does not allow (422).

    The message names the field and the rule — never the submitted value,
    so it is safe to show the user verbatim.
    """


def validate_color(value: object, label: str = "colour") -> str:
    """Accept only ``#RRGGBB``. Returns the lower-cased value."""
    if not isinstance(value, str) or not _HEX_COLOR_RE.fullmatch(value):
        raise ThemeValidationError(f"{label} must be a hex colour like #1a2b3c")
    return value.lower()


def validate_optional_color(value: object, label: str) -> str | None:
    if value is None:
        return None
    return validate_color(value, label)


def validate_choice(value: object, allowed: frozenset[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ThemeValidationError(
            f"{label} must be one of: {', '.join(sorted(allowed))}",
        )
    return value


def validate_optional_choice(
    value: object,
    allowed: frozenset[str],
    label: str,
) -> str | None:
    if value is None:
        return None
    return validate_choice(value, allowed, label)


def validate_corner_radius(value: object) -> int:
    """Accept a real ``int`` or an ASCII digit string (``"12"``) — never a
    bool, float, sign, whitespace, underscore or non-ASCII digit, all of
    which ``int()`` would otherwise quietly coerce."""
    if isinstance(value, bool):
        raise ThemeValidationError("corner_radius must be a whole number")
    if isinstance(value, int):
        v = value
    elif isinstance(value, str) and _ASCII_DIGITS_RE.fullmatch(value):
        v = int(value)
    else:
        raise ThemeValidationError("corner_radius must be a whole number")
    if not (0 <= v <= 24):
        raise ThemeValidationError("corner_radius must be between 0 and 24")
    return v


@dataclass(slots=True, frozen=True)
class HouseholdTheme:
    """Household-wide visual preferences."""

    # Brand-aligned defaults — hearth terracotta + honey, matching
    # ``tokens.css``'s ``--sh-primary`` / ``--sh-warning``.
    primary_color: str = "#D2542A"
    accent_color: str = "#C8902F"
    surface_color: str | None = None
    surface_dark: str | None = None
    mode: str = "auto"
    font_family: str = DEFAULT_FONT
    density: str = "comfortable"
    corner_radius: int = 12
    updated_at: str | None = None


@dataclass(slots=True, frozen=True)
class SpaceTheme:
    """Per-space overrides (§23.123)."""

    space_id: str
    # Brand-aligned defaults — hearth terracotta + honey, matching
    # ``tokens.css``'s ``--sh-primary`` / ``--sh-warning``.
    primary_color: str = "#D2542A"
    accent_color: str = "#C8902F"
    header_image_file: str | None = None
    background_tint: str | None = None
    mode_override: str | None = None
    font_family: str = DEFAULT_FONT
    post_layout: str = DEFAULT_POST_LAYOUT
    updated_at: str | None = None
