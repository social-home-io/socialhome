"""Admin API for the household dashboard (wall tablet on its own port).

* ``GET / PUT /api/admin/dashboard`` — settings + listener status + people;
* ``PUT /api/admin/dashboard/password`` — the household dashboard password;
* ``POST /api/admin/dashboard/setup-link`` — a one-time tablet sign-in link;
* ``DELETE /api/admin/dashboard/sessions`` — sign out every tablet.

Admin-only: every call goes through a
:class:`~socialhome.services.dashboard_settings_service.DashboardSettingsService`
method taking ``actor_is_admin``, whose :class:`PermissionError` maps to 403.
"""

from __future__ import annotations

import ipaddress
import time
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from ..app_keys import (
    dashboard_auth_key,
    dashboard_server_key,
    dashboard_settings_service_key,
    media_signer_key,
    user_service_key,
)
from ..dashboard.auth import DashboardAuth
from ..dashboard.server import DashboardServer
from ..domain.household_dashboard import (
    SETUP_TOKEN_TTL_SECONDS,
    DashboardPortInUseError,
    DashboardSettings,
)
from ..domain.user import User, _picture_url
from ..media_signer import sign_media_urls_in
from ..services.dashboard_settings_service import DashboardSettingsService
from .base import BaseView

__all__ = [
    "AdminDashboardPasswordView",
    "AdminDashboardSessionsView",
    "AdminDashboardSetupLinkView",
    "AdminDashboardView",
    "dashboard_base_url",
]


def _request_hostname(host: str | None) -> str:
    """``host`` from a ``Host`` header without its port (IPv6 bracketed)."""
    text = (host or "").strip().lower()
    if text.startswith("["):
        end = text.find("]")
        if end != -1:
            try:
                ipaddress.IPv6Address(text[1:end])
            except ValueError:
                return "localhost"
            return text[: end + 1]
        return "localhost"
    name, sep, port = text.rpartition(":")
    if sep and port.isdigit():
        text = name
    return text.rstrip(".") or "localhost"


def dashboard_base_url(address: str | None, request_host: str | None, port: int) -> str:
    """Where tablets reach the dashboard.

    The admin-set ``address`` when there is one; otherwise a guess: plain
    ``http`` on the host name the admin reached Social Home under (its
    ``Host`` header, port dropped) and the dashboard ``port`` — on the Home
    Assistant App that is e.g. ``http://homeassistant.local:8100``.
    """
    if address:
        return address
    return f"http://{_request_hostname(request_host)}:{port}"


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _person(user: User, settings: DashboardSettings) -> dict[str, Any]:
    """One household member as the admin UI sees them.

    ``picture_effective`` resolves the "default" (``picture=None``):
    pictures are on by default for child-protected minors. Only that
    boolean leaves — never the age or minor flag behind it.
    """
    stored = settings.person(user.user_id)
    if stored.picture is None:
        effective = bool(user.is_minor and user.child_protection_enabled)
    else:
        effective = stored.picture
    return {
        "user_id": user.user_id,
        "username": user.username,
        "display_name": user.display_name,
        "picture_url": _picture_url(user.user_id, user.picture_hash),
        "show": stored.show,
        "picture": stored.picture,
        "picture_effective": effective,
    }


class _DashboardAdminView(BaseView):
    @property
    def settings(self) -> DashboardSettingsService:
        return self.svc(dashboard_settings_service_key)

    @property
    def server(self) -> DashboardServer:
        return self.svc(dashboard_server_key)

    @property
    def is_admin(self) -> bool:
        user = self.user
        return bool(user is not None and user.is_admin)

    def base_url(self, settings: DashboardSettings) -> str:
        return dashboard_base_url(settings.address, self.request.host, settings.port)

    async def state(self) -> web.Response:
        """The full admin view of the dashboard (``GET`` / ``PUT`` body)."""
        settings = await self.settings.get()
        auth: DashboardAuth = self.svc(dashboard_auth_key)
        users = await self.svc(user_service_key).list_active()
        people = [_person(u, settings) for u in users]
        signer = self.request.app.get(media_signer_key)
        if signer is not None:
            sign_media_urls_in(people, signer)
        running_port = self.server.running_port
        return self._json(
            {
                "enabled": settings.enabled,
                "running": running_port is not None,
                "running_port": running_port,
                "port": settings.port,
                "port_fixed": self.settings.port_fixed,
                "password_set": settings.password_set_at is not None,
                "password_set_at": settings.password_set_at,
                "address": settings.address,
                "url": self.base_url(settings),
                "locale": settings.locale,
                "idle_return_minutes": settings.idle_return_minutes,
                "night_start": settings.night_start,
                "night_end": settings.night_end,
                "keep_screen_on": settings.keep_screen_on,
                "show_status_text": settings.show_status_text,
                "last_seen_at": _iso(auth.last_seen_at),
                "people": people,
            }
        )


class AdminDashboardView(_DashboardAdminView):
    """``GET / PUT /api/admin/dashboard``."""

    async def get(self) -> web.Response:
        # The admin gate lives in the service; ``get`` there is unguarded
        # (the dashboard app reads it), so check here before reading.
        if not self.is_admin:
            raise PermissionError("Only household admins may manage the dashboard")
        return await self.state()

    async def put(self) -> web.Response:
        """Apply a settings patch, then move the listener to match.

        When the listener can't follow (port taken) the previous settings
        are put back and the 409 surfaces; the listener keeps its old
        state (an old port keeps serving).
        """
        patch = await self.body()
        previous = await self.settings.get()
        updated = await self.settings.update(actor_is_admin=self.is_admin, patch=patch)
        try:
            await self.server.apply(updated)
        except DashboardPortInUseError:
            await self.settings.restore(actor_is_admin=True, previous=previous)
            raise
        return await self.state()


class AdminDashboardPasswordView(_DashboardAdminView):
    """``PUT /api/admin/dashboard/password`` — signs out every tablet."""

    async def put(self) -> web.Response:
        body = await self.body()
        password = body.get("password") if isinstance(body, dict) else None
        # A non-string is refused by the service (DASHBOARD_SETTINGS_INVALID).
        await self.settings.set_password(
            actor_is_admin=self.is_admin,
            password=password,  # type: ignore[arg-type]
        )
        return web.Response(status=204)


class AdminDashboardSetupLinkView(_DashboardAdminView):
    """``POST /api/admin/dashboard/setup-link`` — a one-time sign-in link
    for a tablet (shown as a QR code). Replaces any pending link."""

    async def post(self) -> web.Response:
        now = time.time()
        token = await self.settings.issue_setup_token(
            actor_is_admin=self.is_admin, now=now
        )
        settings = await self.settings.get()
        return self._json(
            {
                "url": f"{self.base_url(settings)}/enter?t={token}",
                "expires_at": _iso(now + SETUP_TOKEN_TTL_SECONDS),
            }
        )


class AdminDashboardSessionsView(_DashboardAdminView):
    """``DELETE /api/admin/dashboard/sessions`` — sign out every tablet."""

    async def delete(self) -> web.Response:
        await self.settings.revoke_sessions(actor_is_admin=self.is_admin)
        return web.Response(status=204)
