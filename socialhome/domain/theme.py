"""Theme domain types (§23.123, §23.125)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class HouseholdTheme:
    """Household-wide visual preferences."""

    primary_color: str = "#4A90E2"
    accent_color: str = "#F5A623"
    surface_color: str | None = None
    surface_dark: str | None = None
    mode: str = "auto"
    font_family: str = "system"
    density: str = "comfortable"
    corner_radius: int = 12
    updated_at: str | None = None


@dataclass(slots=True, frozen=True)
class SpaceTheme:
    """Per-space overrides (§23.123)."""

    space_id: str
    primary_color: str = "#4A90E2"
    accent_color: str = "#F5A623"
    header_image_file: str | None = None
    background_tint: str | None = None
    mode_override: str | None = None
    font_family: str = "system"
    post_layout: str = "card"
    updated_at: str | None = None
