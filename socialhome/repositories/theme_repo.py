"""Theme repository — household + per-space visual preferences (§23.125).

Two tables, one row shape each:

* ``household_theme`` — one row keyed on ``id='default'`` that every
  surface inherits from. Carries the household-wide palette, mode
  (light/dark/auto), font, density and corner radius.
* ``space_themes`` — one row per space with the per-space overrides
  (accent colour, header image, background tint, post layout, mode
  override, font override).

The repo enforces hex/enum validation; the service layer enforces
permissions (admins only for household, space admins for a space).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase

# Domain dataclasses + validators live in ``socialhome/domain/theme.py``;
# re-exported here so existing repo-level imports keep working.
from ..domain.theme import (  # noqa: F401
    ALLOWED_DENSITIES,
    ALLOWED_FONTS,
    ALLOWED_MODES,
    ALLOWED_POST_LAYOUTS,
    DEFAULT_FONT,
    DEFAULT_POST_LAYOUT,
    HouseholdTheme,
    SpaceTheme,
    ThemeValidationError,
    validate_choice,
    validate_color,
    validate_corner_radius,
    validate_optional_choice,
    validate_optional_color,
)


def _reset_or_keep(patch: dict, key: str, current: str, default: str) -> object:
    """Absent → keep ``current``; ``null`` → the column ``default``."""
    if key not in patch:
        return current
    value = patch[key]
    return default if value is None else value


@runtime_checkable
class AbstractThemeRepo(Protocol):
    async def get_household(self) -> HouseholdTheme: ...

    async def update_household(self, **patch) -> HouseholdTheme: ...

    async def get_space(self, space_id: str) -> SpaceTheme | None: ...

    async def upsert_space(
        self,
        *,
        space_id: str,
        **patch,
    ) -> SpaceTheme: ...


class SqliteThemeRepo:
    """SQLite-backed :class:`AbstractThemeRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── Household ──────────────────────────────────────────────────────────

    async def get_household(self) -> HouseholdTheme:
        row = await self._db.fetchone(
            "SELECT primary_color, accent_color, surface_color, surface_dark, "
            "mode, font_family, density, corner_radius, updated_at "
            "FROM household_theme WHERE id='default'",
        )
        if row is None:
            return HouseholdTheme()
        return HouseholdTheme(
            primary_color=row["primary_color"],
            accent_color=row["accent_color"],
            surface_color=row["surface_color"],
            surface_dark=row["surface_dark"],
            mode=row["mode"],
            font_family=row["font_family"],
            density=row["density"],
            corner_radius=int(row["corner_radius"]),
            updated_at=row["updated_at"],
        )

    async def update_household(self, **patch) -> HouseholdTheme:
        current = await self.get_household()
        primary = validate_color(
            patch.get("primary_color", current.primary_color), "primary_color"
        )
        accent = validate_color(
            patch.get("accent_color", current.accent_color), "accent_color"
        )
        surface = validate_optional_color(
            patch.get("surface_color", current.surface_color), "surface_color"
        )
        surface_dark = validate_optional_color(
            patch.get("surface_dark", current.surface_dark), "surface_dark"
        )
        mode = validate_choice(patch.get("mode", current.mode), ALLOWED_MODES, "mode")
        font = validate_choice(
            patch.get("font_family", current.font_family),
            ALLOWED_FONTS,
            "font_family",
        )
        density = validate_choice(
            patch.get("density", current.density),
            ALLOWED_DENSITIES,
            "density",
        )
        corner_radius = validate_corner_radius(
            patch.get("corner_radius", current.corner_radius),
        )
        ts = datetime.now(timezone.utc).isoformat()
        await self._db.enqueue(
            """
            INSERT INTO household_theme(
                id, primary_color, accent_color, surface_color, surface_dark,
                mode, font_family, density, corner_radius, updated_at
            ) VALUES('default', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                primary_color=excluded.primary_color,
                accent_color=excluded.accent_color,
                surface_color=excluded.surface_color,
                surface_dark=excluded.surface_dark,
                mode=excluded.mode,
                font_family=excluded.font_family,
                density=excluded.density,
                corner_radius=excluded.corner_radius,
                updated_at=excluded.updated_at
            """,
            (
                primary,
                accent,
                surface,
                surface_dark,
                mode,
                font,
                density,
                corner_radius,
                ts,
            ),
        )
        return HouseholdTheme(
            primary_color=primary,
            accent_color=accent,
            surface_color=surface,
            surface_dark=surface_dark,
            mode=mode,
            font_family=font,
            density=density,
            corner_radius=corner_radius,
            updated_at=ts,
        )

    # ── Space ──────────────────────────────────────────────────────────────

    async def get_space(self, space_id: str) -> SpaceTheme | None:
        row = await self._db.fetchone(
            "SELECT primary_color, accent_color, header_image_file, "
            "background_tint, mode_override, font_family, post_layout, "
            "updated_at "
            "FROM space_themes WHERE space_id=?",
            (space_id,),
        )
        if row is None:
            return None
        return SpaceTheme(
            space_id=space_id,
            primary_color=row["primary_color"],
            accent_color=row["accent_color"],
            header_image_file=row["header_image_file"],
            background_tint=row["background_tint"],
            mode_override=row["mode_override"],
            font_family=row["font_family"],
            post_layout=row["post_layout"],
            updated_at=row["updated_at"],
        )

    async def upsert_space(self, *, space_id: str, **patch) -> SpaceTheme:
        current = await self.get_space(space_id) or SpaceTheme(space_id=space_id)
        primary = validate_color(
            patch.get("primary_color", current.primary_color), "primary_color"
        )
        accent = validate_color(
            patch.get("accent_color", current.accent_color), "accent_color"
        )
        header = patch.get("header_image_file", current.header_image_file)
        if header is not None and not isinstance(header, str):
            raise ThemeValidationError("header_image_file must be a string or null")
        tint = validate_optional_color(
            patch.get("background_tint", current.background_tint),
            "background_tint",
        )
        mode_override = validate_optional_choice(
            patch.get("mode_override", current.mode_override),
            ALLOWED_MODES,
            "mode_override",
        )
        # ``font_family`` / ``post_layout`` are non-null columns with a
        # default ("system" / "card"). Absent keeps the current value;
        # ``null`` means "no override" and resets to the column default.
        font = validate_choice(
            _reset_or_keep(patch, "font_family", current.font_family, DEFAULT_FONT),
            ALLOWED_FONTS,
            "font_family",
        )
        layout = validate_choice(
            _reset_or_keep(
                patch, "post_layout", current.post_layout, DEFAULT_POST_LAYOUT
            ),
            ALLOWED_POST_LAYOUTS,
            "post_layout",
        )
        ts = datetime.now(timezone.utc).isoformat()
        await self._db.enqueue(
            """
            INSERT INTO space_themes(
                space_id, primary_color, accent_color, header_image_file,
                background_tint, mode_override, font_family, post_layout,
                updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(space_id) DO UPDATE SET
                primary_color=excluded.primary_color,
                accent_color=excluded.accent_color,
                header_image_file=excluded.header_image_file,
                background_tint=excluded.background_tint,
                mode_override=excluded.mode_override,
                font_family=excluded.font_family,
                post_layout=excluded.post_layout,
                updated_at=excluded.updated_at
            """,
            (space_id, primary, accent, header, tint, mode_override, font, layout, ts),
        )
        return SpaceTheme(
            space_id=space_id,
            primary_color=primary,
            accent_color=accent,
            header_image_file=header,
            background_tint=tint,
            mode_override=mode_override,
            font_family=font,
            post_layout=layout,
            updated_at=ts,
        )
