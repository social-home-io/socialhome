"""ThemeService — extended household + space theming (§23.123, §23.125).

Route handlers call :meth:`update_household_theme` and
:meth:`update_space_theme` with a partial dict of fields. The repo
validates each field and keeps the previous value where the patch
omits it.
"""

from __future__ import annotations

from ..domain.space import SpacePermissionError
from ..repositories.theme_repo import (
    AbstractThemeRepo,
    HouseholdTheme,
    SpaceTheme,
)


#: Household fields callers are permitted to patch.
_HOUSEHOLD_ALLOWED = frozenset(
    {
        "primary_color",
        "accent_color",
        "surface_color",
        "surface_dark",
        "mode",
        "font_family",
        "density",
        "corner_radius",
    }
)

#: Space fields callers are permitted to patch.
_SPACE_ALLOWED = frozenset(
    {
        "primary_color",
        "accent_color",
        "header_image_file",
        "background_tint",
        "mode_override",
        "font_family",
        "post_layout",
    }
)


class ThemeService:
    """Public API for theme reads + writes."""

    __slots__ = ("_repo", "_space_repo")

    def __init__(self, repo: AbstractThemeRepo, space_repo) -> None:
        self._repo = repo
        self._space_repo = space_repo

    # ─── Household ────────────────────────────────────────────────────────

    async def get_household_theme(self) -> HouseholdTheme:
        return await self._repo.get_household()

    async def update_household_theme(
        self,
        *,
        actor_user_id: str,
        actor_is_admin: bool,
        patch: dict,
    ) -> HouseholdTheme:
        if not actor_is_admin:
            raise SpacePermissionError(
                "Only admins may change the household theme",
            )
        safe = {k: v for k, v in (patch or {}).items() if k in _HOUSEHOLD_ALLOWED}
        return await self._repo.update_household(**safe)

    # ─── Space ────────────────────────────────────────────────────────────

    async def get_space_theme(self, space_id: str) -> SpaceTheme | None:
        return await self._repo.get_space(space_id)

    async def update_space_theme(
        self,
        *,
        space_id: str,
        actor_user_id: str,
        patch: dict,
    ) -> SpaceTheme:
        # Only space owner / admin may set the space theme.
        member = await self._space_repo.get_member(space_id, actor_user_id)
        if member is None or member.role not in ("owner", "admin"):
            raise SpacePermissionError(
                "Only space owners/admins may change the space theme",
            )
        safe = {k: v for k, v in (patch or {}).items() if k in _SPACE_ALLOWED}
        return await self._repo.upsert_space(space_id=space_id, **safe)
