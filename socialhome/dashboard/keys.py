"""Typed ``app`` / ``request`` keys shared by the dashboard app and views."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiohttp import web

from ..services.dashboard_settings_service import DashboardSettingsService
from .auth import DashboardAuth, SessionClaims
from .render import DashboardRenderer

__all__ = [
    "AUTH_KEY",
    "CLAIMS_KEY",
    "HOUSEHOLD_NAME_KEY",
    "HOUSEHOLD_TZ_KEY",
    "HouseholdNameFn",
    "HouseholdTzFn",
    "RENDERER_KEY",
    "SETTINGS_KEY",
]

#: Returns the household's display name (``HouseholdPreferences``).
HouseholdNameFn = Callable[[], Awaitable[str]]
#: Returns the household's IANA time zone (``HouseholdPreferences.tz``).
HouseholdTzFn = Callable[[], Awaitable[str]]

AUTH_KEY: web.AppKey[DashboardAuth] = web.AppKey("dashboard_auth")
SETTINGS_KEY: web.AppKey[DashboardSettingsService] = web.AppKey("dashboard_settings")
RENDERER_KEY: web.AppKey[DashboardRenderer] = web.AppKey("dashboard_renderer")
HOUSEHOLD_NAME_KEY: web.AppKey[HouseholdNameFn] = web.AppKey("dashboard_household_name")
HOUSEHOLD_TZ_KEY: web.AppKey[HouseholdTzFn] = web.AppKey("dashboard_household_tz")
#: The verified session of the current request (set by the session guard).
CLAIMS_KEY: web.RequestKey[SessionClaims] = web.RequestKey("dashboard_claims")
