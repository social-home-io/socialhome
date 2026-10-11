"""Typed ``app`` / ``request`` keys shared by the dashboard app and views."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol

from aiohttp import web

from ..domain.household_dashboard import HomeSnapshot, PersonSnapshot
from ..domain.theme import HouseholdTheme
from ..services.dashboard_settings_service import DashboardSettingsService
from .auth import DashboardAuth, SessionClaims
from .render import DashboardRenderer

__all__ = [
    "AUTH_KEY",
    "CLAIMS_KEY",
    "HOUSEHOLD_NAME_KEY",
    "HOUSEHOLD_THEME_KEY",
    "HOUSEHOLD_TZ_KEY",
    "DashboardSnapshots",
    "HouseholdNameFn",
    "HouseholdThemeFn",
    "HouseholdTzFn",
    "RENDERER_KEY",
    "SETTINGS_KEY",
    "SNAPSHOTS_KEY",
]

#: Returns the household's display name (``HouseholdPreferences``).
HouseholdNameFn = Callable[[], Awaitable[str]]
#: Returns the household's IANA time zone (``HouseholdPreferences.tz``).
HouseholdTzFn = Callable[[], Awaitable[str]]
#: Returns the household theme (``ThemeService.get_household_theme``).
HouseholdThemeFn = Callable[[], Awaitable[HouseholdTheme]]


class DashboardSnapshots(Protocol):
    """The read model the pages render
    (:class:`~socialhome.services.household_dashboard_service.HouseholdDashboardService`)."""

    async def home(self, *, now: datetime | None = None) -> HomeSnapshot: ...

    async def person(
        self, user_id: str, *, now: datetime | None = None
    ) -> PersonSnapshot | None: ...

    async def avatar(self, user_id: str) -> tuple[bytes, str] | None: ...


AUTH_KEY: web.AppKey[DashboardAuth] = web.AppKey("dashboard_auth")
SETTINGS_KEY: web.AppKey[DashboardSettingsService] = web.AppKey("dashboard_settings")
RENDERER_KEY: web.AppKey[DashboardRenderer] = web.AppKey("dashboard_renderer")
HOUSEHOLD_NAME_KEY: web.AppKey[HouseholdNameFn] = web.AppKey("dashboard_household_name")
HOUSEHOLD_TZ_KEY: web.AppKey[HouseholdTzFn] = web.AppKey("dashboard_household_tz")
HOUSEHOLD_THEME_KEY: web.AppKey[HouseholdThemeFn] = web.AppKey(
    "dashboard_household_theme"
)
SNAPSHOTS_KEY: web.AppKey[DashboardSnapshots] = web.AppKey("dashboard_snapshots")
#: The verified session of the current request (set by the session guard).
CLAIMS_KEY: web.RequestKey[SessionClaims] = web.RequestKey("dashboard_claims")
