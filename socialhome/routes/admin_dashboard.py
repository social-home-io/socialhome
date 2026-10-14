"""Admin API for the household dashboard (wall tablet on its own port).

* ``GET / PUT /api/admin/dashboard`` — settings + listener status + people;
* ``PUT /api/admin/dashboard/password`` — the household dashboard password;
* ``POST /api/admin/dashboard/setup-link`` — a one-time tablet sign-in link
  (only while the dashboard runs);
* ``DELETE /api/admin/dashboard/sessions`` — sign out every tablet;
* ``PUT / DELETE /api/admin/dashboard/pin`` — the tablets' privacy-lock
  PIN (4–6 digits, separate from the password; never read back);
* ``GET / POST /api/admin/dashboard/displays`` and ``PATCH / DELETE
  /api/admin/dashboard/displays/{display_id}`` — wall panels: list, pair
  one by the code it shows, rename, unpair. A panel's token (and its
  hash) never appears here — the panel collects the token itself.
* ``GET /api/admin/dashboard/displays/ha`` and ``POST
  /api/admin/dashboard/displays/ha/{panel_id}/connect`` — wall panels
  adopted in Home Assistant (only with ``Capability.HA_DISPLAY_DISCOVERY``,
  else ``404 DISPLAY_DISCOVERY_UNAVAILABLE``): list them, and send one a
  connect request (a single-use claim + the dashboard address it should
  spend it at) — the panel pairs itself.

Admin-only: every call goes through a
:class:`~socialhome.services.dashboard_settings_service.DashboardSettingsService`
method taking ``actor_is_admin``, whose :class:`PermissionError` maps to 403.
"""

from __future__ import annotations

import ipaddress
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from ..app_keys import (
    dashboard_auth_key,
    dashboard_controller_key,
    dashboard_display_service_key,
    media_signer_key,
    platform_adapter_key,
    preferences_service_key,
    user_service_key,
)
from ..dashboard.auth import DashboardAuth
from ..dashboard.controller import DashboardController
from ..domain.household_dashboard import (
    SETUP_TOKEN_TTL_SECONDS,
    DashboardAddressRequiredError,
    DashboardDisabledError,
    DashboardDisplay,
    DashboardSettings,
    DisplayDiscoveryUnavailableError,
    ExternalDisplayPanel,
    picture_effective,
)
from ..domain.user import User, _picture_url
from ..media_signer import sign_media_urls_in
from ..platform.adapter import DashboardHostPort
from ..services.dashboard_display_service import DashboardDisplayService
from ..services.dashboard_settings_service import DashboardSettingsService
from ..services.system_chat_policy import HOUSEHOLD_CHAT_SECTION
from .base import BaseView

__all__ = [
    "AdminDashboardDisplayView",
    "AdminDashboardHaPanelConnectView",
    "AdminDashboardHaPanelsView",
    "AdminDashboardDisplaysView",
    "AdminDashboardPasswordView",
    "AdminDashboardPinView",
    "AdminDashboardSessionsView",
    "AdminDashboardSetupLinkView",
    "AdminDashboardView",
    "admin_request_host",
    "dashboard_base_url",
    "panel_url",
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


#: Host names a wall panel can never reach the dashboard under.
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})


def _unusable_guess(host: str | None) -> bool:
    """A guessed host no panel should be sent to: a loopback name, or a
    public IP literal (the admin reached Social Home over the internet —
    the claim and the panel's token would cross it in plain ``http``)."""
    name = (host or "").strip("[]").rstrip(".").lower()
    if not name or name in _LOOPBACK_NAMES or name.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_global


def panel_url(
    address: str | None, guessed: str, host_port: DashboardHostPort | None
) -> str:
    """The dashboard address a wall panel connected from Home Assistant
    should use: the admin-set ``address``, else the ``guessed`` tablet URL
    (:func:`dashboard_base_url`) — unless the Home Assistant App's
    dashboard port isn't published on the host
    (:class:`DashboardAddressRequiredError` ``port_not_published``) or the
    guess is a loopback name no panel can reach, or a public IP the claim
    shouldn't travel to in plain ``http`` (``no_address``)."""
    if address:
        return address
    if host_port is not None and host_port.declared and not host_port.mapped:
        raise DashboardAddressRequiredError("port_not_published")
    if _unusable_guess(urlsplit(guessed).hostname):
        raise DashboardAddressRequiredError("no_address")
    return guessed


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


def _display(display: DashboardDisplay) -> dict[str, Any]:
    """One wall panel as the admin UI sees it — never its token hash."""
    return {
        "id": display.id,
        "name": display.name,
        "model": display.model,
        "firmware": display.firmware,
        "created_at": display.created_at,
        "last_seen_at": display.last_seen_at,
        "locked": display.locked,
    }


def _ha_panel(
    panel: ExternalDisplayPanel, display: DashboardDisplay | None
) -> dict[str, Any]:
    """One panel found in Home Assistant as the admin UI sees it.
    ``status``: ``connected`` (paired here — ``display_id`` / ``display_name``),
    ``ready`` (Connect works), ``offline`` (HA lost it), ``needs_update``
    (its firmware has no connect action, or publishes no key to seal the
    connect request to). Never the action name, key or MAC."""
    connectable = (
        panel.connect_action is not None
        and panel.mac is not None
        and panel.panel_key is not None
    )
    if display is not None:
        status = "connected"
    elif not panel.online:
        status = "offline"
    elif not connectable:
        status = "needs_update"
    else:
        status = "ready"
    return {
        "id": panel.id,
        "name": panel.name,
        "sw_version": panel.sw_version,
        "status": status,
        # A connected panel can be connected again (e.g. after a reset)
        # while Home Assistant can reach it.
        "can_connect": panel.online and connectable,
        "display_id": display.id if display is not None else None,
        "display_name": display.name if display is not None else None,
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
    here: the endpoint is admin-only, and the value shapes a URL shown back
    to that admin — or, for Connect from Home Assistant, sent to a wall
    panel, where :func:`panel_url` refuses loopback and public-IP guesses
    (no cross-site page can set this header on the admin's request).
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
    def displays(self) -> DashboardDisplayService:
        return self.svc(dashboard_display_service_key)

    @property
    def is_admin(self) -> bool:
        user = self.user
        return bool(user is not None and user.is_admin)

    async def host_port(self) -> DashboardHostPort | None:
        """Where the platform publishes the dashboard on the host —
        ``None`` when that's simply the listen port (or unknown)."""
        result: DashboardHostPort | None = await self.svc(
            platform_adapter_key
        ).dashboard_host_port()
        return result

    def base_url(
        self, settings: DashboardSettings, host_port: DashboardHostPort | None
    ) -> str:
        """The tablet URL — on the port the platform publishes the
        dashboard on when it maps one (Home Assistant App: the host port
        set under the App's Network settings), else the listen port."""
        port = settings.port
        if host_port is not None and host_port.port is not None:
            port = host_port.port
        return dashboard_base_url(
            settings.address, admin_request_host(self.request), port
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
        host_port = await self.host_port()
        url = self.base_url(settings, host_port)
        prefs = await self.svc(preferences_service_key).get_household()
        displays = await self.displays.list(actor_is_admin=self.is_admin)
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
                # ``False`` when the platform offers a host port the user
                # must turn on (Home Assistant App: Settings › Apps ›
                # Social Home › Network); ``null`` when not applicable,
                # unknown, or the platform doesn't offer the port at all.
                "host_port_mapped": (
                    host_port.mapped
                    if host_port is not None and host_port.declared
                    else None
                ),
                "locale": settings.locale,
                "idle_return_minutes": settings.idle_return_minutes,
                "night_start": settings.night_start,
                "night_end": settings.night_end,
                "keep_screen_on": settings.keep_screen_on,
                "show_status_text": settings.show_status_text,
                "show_household_messages": settings.show_household_messages,
                "mention_sound": settings.mention_sound,
                # The messages come from the household chat: the admin UI
                # greys the switch while the chat itself is off.
                "household_chat_enabled": prefs.is_enabled(HOUSEHOLD_CHAT_SECTION),
                "last_seen_at": _iso(auth.last_seen_at),
                "people": people,
                "displays": [_display(d) for d in displays],
                # Wall panels adopted in Home Assistant can be listed and
                # connected (``GET …/displays/ha``) — ha / haos only.
                "ha_display_discovery": self.displays.can_discover,
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
        base = self.base_url(settings, await self.host_port())
        return self._json(
            {
                "url": f"{base}/enter?t={token}",
                "expires_at": _iso(now + SETUP_TOKEN_TTL_SECONDS),
            }
        )


class AdminDashboardSessionsView(_DashboardAdminView):
    """``DELETE /api/admin/dashboard/sessions`` — sign out every tablet."""

    async def delete(self) -> web.Response:
        await self.settings.revoke_sessions(actor_is_admin=self.is_admin)
        return web.Response(status=204)


class AdminDashboardDisplaysView(_DashboardAdminView):
    """``GET / POST /api/admin/dashboard/displays`` — list the wall panels;
    pair the one showing ``code`` as ``name`` (``201``)."""

    async def get(self) -> web.Response:
        displays = await self.displays.list(actor_is_admin=self.is_admin)
        return self._json({"displays": [_display(d) for d in displays]})

    async def post(self) -> web.Response:
        body = await self.body()
        fields = body if isinstance(body, dict) else {}
        display = await self.displays.claim(
            actor_is_admin=self.is_admin,
            code=fields.get("code"),
            name=fields.get("name"),
        )
        return self._json(_display(display), status=201)


class AdminDashboardDisplayView(_DashboardAdminView):
    """``PATCH / DELETE /api/admin/dashboard/displays/{display_id}`` —
    rename a panel (``name``) and / or lift its privacy lock (``locked:
    false`` — only the panel itself locks, with the PIN set); unpair it
    (its token stops working at once)."""

    async def patch(self) -> web.Response:
        body = await self.body()
        display = await self.displays.update(
            actor_is_admin=self.is_admin,
            display_id=self.request.match_info["display_id"],
            patch=body if isinstance(body, dict) else {},
        )
        return self._json(_display(display))

    async def delete(self) -> web.Response:
        await self.displays.remove(
            actor_is_admin=self.is_admin,
            display_id=self.request.match_info["display_id"],
        )
        return web.Response(status=204)


class AdminDashboardHaPanelsView(_DashboardAdminView):
    """``GET /api/admin/dashboard/displays/ha`` — wall panels adopted in
    Home Assistant, each with whether it is paired here. ``404
    DISPLAY_DISCOVERY_UNAVAILABLE`` without the capability; ``502
    DISPLAY_DISCOVERY_FAILED`` when Home Assistant didn't answer."""

    async def get(self) -> web.Response:
        found = await self.displays.platform_panels(actor_is_admin=self.is_admin)
        return self._json({"panels": [_ha_panel(p, d) for p, d in found]})


class AdminDashboardHaPanelConnectView(_DashboardAdminView):
    """``POST /api/admin/dashboard/displays/ha/{panel_id}/connect`` — send
    the panel a connect request through Home Assistant: ``202
    {expires_in}``; the panel then pairs itself (it shows up under the
    paired panels). ``409 DASHBOARD_ADDRESS_REQUIRED`` when there is no
    dashboard address a panel could use."""

    async def post(self) -> web.Response:
        if not self.is_admin:
            raise PermissionError("Only household admins may manage the dashboard")
        if not self.displays.can_discover:
            raise DisplayDiscoveryUnavailableError()
        if self.controller.running_port is None:
            raise DashboardDisabledError()
        settings = await self.settings.get()
        host_port = await self.host_port()
        url = panel_url(settings.address, self.base_url(settings, host_port), host_port)
        if not await self.controller.url_host_allowed(url):
            # The dashboard would refuse the panel's Host (421).
            raise DashboardAddressRequiredError("no_address")
        expires_in = await self.displays.connect_platform_panel(
            actor_is_admin=self.is_admin,
            panel_id=self.request.match_info["panel_id"],
            url=url,
        )
        return self._json({"expires_in": expires_in}, status=202)
