"""Admin API for the household dashboard (wall tablet on its own port).

* ``GET / PUT /api/admin/dashboard`` — settings + listener status + people;
* ``PUT /api/admin/dashboard/password`` — the household dashboard password;
* ``POST /api/admin/dashboard/setup-link`` — a one-time tablet sign-in link
  (only while the dashboard runs);
* ``DELETE /api/admin/dashboard/sessions`` — sign out every tablet;
* ``PUT / DELETE /api/admin/dashboard/pin`` — the tablets' privacy-lock
  PIN (4–6 digits, separate from the password; never read back).

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
    dashboard_controller_key,
    media_signer_key,
    user_service_key,
)
from ..dashboard.auth import DashboardAuth
from ..dashboard.controller import DashboardController
from ..domain.household_dashboard import (
    SETUP_TOKEN_TTL_SECONDS,
    DashboardSettings,
    picture_effective,
)
from ..domain.user import User, _picture_url
from ..media_signer import sign_media_urls_in
from ..services.dashboard_settings_service import DashboardSettingsService
from .base import BaseView

__all__ = [
    "AdminDashboardPasswordView",
    "AdminDashboardPinView",
    "AdminDashboardSessionsView",
    "AdminDashboardSetupLinkView",
    "AdminDashboardView",
    "admin_request_host",
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

    ``picture_effective`` resolves the "default" (``picture=None``) with
    the shared :func:`~socialhome.domain.household_dashboard.picture_effective`
    rule. Only that boolean leaves — never the age or minor flag behind it.
    """
    stored = settings.person(user.user_id)
    return {
        "user_id": user.user_id,
        "username": user.username,
        "display_name": user.display_name,
        "picture_url": _picture_url(user.user_id, user.picture_hash),
        "show": stored.show,
        "picture": stored.picture,
        "picture_effective": picture_effective(stored, user),
    }


def admin_request_host(request: web.BaseRequest) -> str:
    """The host the admin's browser used to reach Social Home.

    ``X-Forwarded-Host`` (first entry) wins over ``Host`` — the convention
    of ``hardening.py``'s CORS check and ``routes/ws.py``. Under the Home
    Assistant App the request reaches us through HA Core's ingress proxy
    (``homeassistant/components/hassio/ingress.py`` ``_init_header``: copies
    the client's headers, ``Host`` included, and sets ``X-Forwarded-Host``
    to the incoming ``X-Forwarded-Host`` or else the request's host) and
    then Supervisor (``supervisor/api/ingress.py`` ``_init_header``: passes
    ``Host`` / ``X-Forwarded-Host`` through untouched, only appends to
    ``X-Forwarded-For``), so ``X-Forwarded-Host`` is the name the browser
    typed, e.g. ``homeassistant.local:8123``. Trusting the header is safe
    here: the endpoint is admin-only and the value only shapes a URL shown
    back to that admin.
    """
    forwarded = request.headers.get("X-Forwarded-Host", "").split(",", 1)[0].strip()
    return forwarded or request.host


class _DashboardAdminView(BaseView):
    @property
    def controller(self) -> DashboardController:
        return self.svc(dashboard_controller_key)

    @property
    def settings(self) -> DashboardSettingsService:
        return self.controller.settings

    @property
    def is_admin(self) -> bool:
        user = self.user
        return bool(user is not None and user.is_admin)

    def base_url(self, settings: DashboardSettings) -> str:
        return dashboard_base_url(
            settings.address, admin_request_host(self.request), settings.port
        )

    async def state(self) -> web.Response:
        """The full admin view of the dashboard (``GET`` / ``PUT`` body)."""
        settings = await self.settings.get()
        auth: DashboardAuth = self.svc(dashboard_auth_key)
        users = await self.svc(user_service_key).list_active()
        people = [_person(u, settings) for u in users]
        signer = self.request.app.get(media_signer_key)
        if signer is not None:
            sign_media_urls_in(people, signer)
        running_port = self.controller.running_port
        url = self.base_url(settings)
        return self._json(
            {
                "enabled": settings.enabled,
                "running": running_port is not None,
                "running_port": running_port,
                "port": settings.port,
                "port_fixed": self.settings.port_fixed,
                "password_set": settings.password_set_at is not None,
                "password_set_at": settings.password_set_at,
                "pin_set": settings.pin_set,
                "address": settings.address,
                "url": url,
                # ``False`` when the dashboard would refuse ``url``'s host
                # (421, DNS-rebinding guard): the admin should set the address.
                "url_host_allowed": await self.controller.url_host_allowed(url),
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
        """Save a settings patch; the listener follows ``enabled`` / ``port``
        (see :meth:`DashboardController.update` — a port it can't bind is
        409 and nothing is saved)."""
        patch = await self.body()
        await self.controller.update(actor_is_admin=self.is_admin, patch=patch)
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


class AdminDashboardPinView(_DashboardAdminView):
    """``PUT / DELETE /api/admin/dashboard/pin`` — the privacy-lock PIN.

    Setting it signs nobody out; deleting it lifts the lock on every
    locked tablet (nothing could unlock them otherwise).
    """

    async def put(self) -> web.Response:
        body = await self.body()
        pin = body.get("pin") if isinstance(body, dict) else None
        # Anything but 4–6 ASCII digits is 422 DASHBOARD_PIN_INVALID.
        await self.settings.set_pin(
            actor_is_admin=self.is_admin,
            pin=pin,  # type: ignore[arg-type]
        )
        return web.Response(status=204)

    async def delete(self) -> web.Response:
        await self.settings.delete_pin(actor_is_admin=self.is_admin)
        return web.Response(status=204)


class AdminDashboardSetupLinkView(_DashboardAdminView):
    """``POST /api/admin/dashboard/setup-link`` — a one-time sign-in link
    for a tablet (shown as a QR code). Replaces any pending link."""

    async def post(self) -> web.Response:
        now = time.time()
        token = await self.controller.issue_setup_token(
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
