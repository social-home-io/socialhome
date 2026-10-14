"""The household dashboard's aiohttp application.

Served on its own port (:class:`~socialhome.dashboard.server.DashboardServer`)
for a wall tablet. It shares nothing with the main app's request
pipeline: no ``auth_middleware``, no bearer tokens and **no** HA ingress
headers — on this port ``X-Hass-Source`` / ``X-Remote-User-Name`` come
straight from the network and are spoofable. The only credential is the
signed session cookie (:mod:`socialhome.dashboard.auth`) — except under
``/api/display/v1/``, the wall-panel API, where the only credential is
a paired panel's bearer token (:mod:`socialhome.dashboard.display_api`);
the two never cross.

Middleware order: ``Host`` allow-list (DNS rebinding) → body-size cap →
same-origin check on ``POST`` (not for the panel API, which has no
cookie authority) → the session guard.

A privacy-locked session (``SessionClaims.locked``) reaches only
``/lock`` and ``/unlock``; the guard answers every other non-public
route itself — the lock screen for a page view, a marker for a
``/fragment/…`` (:data:`~.views.LOCKED_FRAGMENT`), ``423`` for anything
else (``/avatar/…``, ``/tiles/…``, writes) — so no view can leak a
person, nor a map tile the area the wall was looking at.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace
from urllib.parse import quote, urlsplit

from aiohttp import web

from ..hardening import build_body_size_middleware, install_security_headers
from ..services.dashboard_display_service import DashboardDisplayService
from ..services.dashboard_settings_service import DashboardSettingsService
from .auth import COOKIE_NAME, REVOKED_LOCKED, REVOKED_SIGNED_OUT, DashboardAuth
from .display_api import (
    guard_display_request,
    is_display_api,
    register_display_routes,
)
from .avatars import AvatarImages
from .emoji import EmojiImages
from .map_png import MapPictures
from .keys import (
    AUTH_KEY,
    AVATARS_KEY,
    EMOJI_KEY,
    CLAIMS_KEY,
    DISPLAYS_KEY,
    HOUSEHOLD_NAME_KEY,
    HOUSEHOLD_THEME_KEY,
    HOUSEHOLD_TZ_KEY,
    RENDERER_KEY,
    SETTINGS_KEY,
    SNAPSHOTS_KEY,
    MAP_PICTURES_KEY,
    TILES_KEY,
    DashboardSnapshots,
    DashboardTiles,
    HouseholdNameFn,
    HouseholdThemeFn,
    HouseholdTzFn,
)
from .policy import DASHBOARD_CSP_DIRECTIVES, build_dashboard_csp
from .render import DashboardRenderer
from .views import (
    LOCKED_FRAGMENT,
    LOCKED_HEADER,
    redirect,
    register_routes,
    render_lock_screen,
    set_session_cookie,
)

log = logging.getLogger(__name__)

__all__ = [
    "DASHBOARD_CSP_DIRECTIVES",
    "MAX_BODY_BYTES",
    "DEFAULT_LOCAL_HOSTNAMES",
    "PUBLIC_PATHS",
    "ExtraHosts",
    "build_dashboard_app",
    "build_dashboard_csp",
    "host_allowed",
    "normalise_host",
]

#: Extra host names the dashboard answers to (e.g. an admin-set address):
#: a fixed iterable, or an async callable read per request.
ExtraHosts = Iterable[str] | Callable[[], Awaitable[frozenset[str]]]

#: Single-label names always accepted in ``Host`` (the HA OS default).
DEFAULT_LOCAL_HOSTNAMES = frozenset({"homeassistant"})

#: Home-network name suffixes always accepted in ``Host``.
LOCAL_HOST_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")

#: The dashboard only ever receives a login form.
MAX_BODY_BYTES = 16 * 1024

#: Reachable without a session (``/static/…`` is matched by prefix).
#: ``/logout`` is public so a tablet whose session was revoked can still
#: clear its stale cookie.
PUBLIC_PATHS = frozenset({"/login", "/enter", "/logout", "/healthz"})
_STATIC_PREFIX = "/static/"
_FRAGMENT_PREFIX = "/fragment/"
_AVATAR_PREFIX = "/avatar/"
_TILES_PREFIX = "/tiles/"
#: Loaded by ``<img>`` / page JS: a missing session is a plain 401, never
#: the login redirect, and a locked tablet gets 423 / the marker.
_ASSET_PREFIXES = (_AVATAR_PREFIX, _TILES_PREFIX)
#: The only non-public routes a privacy-locked tablet reaches.
_LOCK_PATHS = frozenset({"/lock", "/unlock"})

_PAGE_HEADERS: dict[str, str] = {
    "X-Frame-Options": "DENY",
    # Not ``no-referrer``: under it browsers send ``Origin: null`` on our
    # own form POSTs, which the same-origin check must refuse. Nothing
    # leaves the origin either way — the dashboard links nowhere else.
    "Referrer-Policy": "same-origin",
    # Wake lock keeps the wall tablet's screen on (dashboard.js).
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), screen-wake-lock=(self)"
    ),
}


_DEFAULT_CSP = build_dashboard_csp()


async def _dashboard_headers(
    request: web.BaseRequest, response: web.StreamResponse
) -> None:
    for name, value in _PAGE_HEADERS.items():
        response.headers[name] = value
    response.headers.setdefault("Content-Security-Policy", _DEFAULT_CSP)
    if request.path.startswith(_STATIC_PREFIX):
        return
    if (
        (request.path.startswith(_ASSET_PREFIXES) or is_display_api(request.path))
        and response.status in (200, 304)
        and "Cache-Control" in response.headers
    ):
        # A shown person's picture or a map tile (``private, no-cache``),
        # or a panel API answer revalidated by ``ETag``: the view set it.
        return
    # Pages, redirects (which may carry Set-Cookie) and errors are
    # per-session — never cache them; only static files and avatars may be.
    response.headers["Cache-Control"] = "no-store"


def normalise_host(name: str) -> str:
    """A host name as the allow-list compares it (lower-case, no root dot)."""
    return name.strip().lower().rstrip(".")


def host_allowed(host: str, extra: frozenset[str]) -> bool:
    """May the dashboard answer a request whose ``Host`` is ``host``?

    The port is ignored. Accepted: IP literals (IPv6 bracketed),
    ``localhost``, home-network suffixes (:data:`LOCAL_HOST_SUFFIXES`) and
    the names in ``extra`` (already normalised: the machine's own
    hostnames, ``homeassistant`` and any admin-set address). Other
    single-label names are refused — that includes the decimal / hex
    IPv4 spellings (``2130706433``). Everything else is refused, so a public DNS
    name rebound to the tablet's LAN address can't reach the dashboard
    with the victim browser's cookie.
    """
    text = host.strip().lower()
    if text.startswith("["):
        end = text.find("]")
        rest = text[end + 1 :]
        if end == -1 or (rest and re.fullmatch(r":\d{1,5}", rest) is None):
            return False
        try:
            ipaddress.IPv6Address(text[1:end])
        except ValueError:
            return False
        return True
    name, sep, port = text.rpartition(":")
    if not sep:
        name = text
    elif not port.isdigit() or ":" in name:
        return False
    name = name.rstrip(".")
    if not name:
        return False
    if name == "localhost" or name in DEFAULT_LOCAL_HOSTNAMES or name in extra:
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        return True
    labels = name.split(".")
    if not all(_LABEL.fullmatch(label) for label in labels):
        return False
    return len(labels) > 1 and name.endswith(LOCAL_HOST_SUFFIXES)


def _build_host_middleware(extra_hosts: ExtraHosts, local_hostnames: Iterable[str]):
    local = frozenset(normalise_host(h) for h in local_hostnames)
    if callable(extra_hosts):
        dynamic = extra_hosts

        async def allowed_extra() -> frozenset[str]:
            return local | frozenset(normalise_host(h) for h in await dynamic())

    else:
        fixed = local | frozenset(normalise_host(h) for h in extra_hosts)

        async def allowed_extra() -> frozenset[str]:
            return fixed

    @web.middleware
    async def middleware(request: web.Request, handler) -> web.StreamResponse:
        if not host_allowed(request.host, await allowed_extra()):
            log.info("household dashboard: refused request for a foreign Host")
            return web.Response(status=421, text="Misdirected Request")
        return await handler(request)

    return middleware


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(_STATIC_PREFIX)


def _same_origin(request: web.Request) -> bool:
    """``Origin`` (else ``Referer``) names this very host.

    Only the ``Host`` header is compared — never ``X-Forwarded-Host``,
    which a client could set.
    """
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source:
        return False
    try:
        netloc = urlsplit(source).netloc
    except ValueError:
        return False
    return bool(netloc) and netloc.lower() == request.host.lower()


@web.middleware
async def _csrf_middleware(request: web.Request, handler) -> web.StreamResponse:
    # The panel API never reads the cookie (bearer token only), so a
    # cross-site request has no ambient credential to ride on there.
    if (
        request.method == "POST"
        and not is_display_api(request.path)
        and not _same_origin(request)
    ):
        log.info("household dashboard: refused cross-origin POST to %s", request.path)
        return web.Response(status=403, text="Forbidden")
    return await handler(request)


@web.middleware
async def _session_guard(request: web.Request, handler) -> web.StreamResponse:
    if is_display_api(request.path):
        # Bearer token only — the tablet cookie is never consulted here.
        return await guard_display_request(request, handler)
    if _is_public(request.path):
        return await handler(request)
    auth = request.app[AUTH_KEY]
    claims = await auth.verify(request.cookies.get(COOKIE_NAME))
    if claims is None:
        # Only a page view (GET) is sent to the login form; HEAD, POST and
        # ``/fragment/`` requests (fetched by page JS) get a plain 401.
        if request.method == "GET" and not request.path.startswith(
            (_FRAGMENT_PREFIX, _TILES_PREFIX)
        ):
            return redirect(
                "/login?next=" + quote(request.raw_path, safe=""), status=302
            )
        return web.Response(status=401, text="Unauthorized")
    auth.touch()
    if claims.locked and not (await request.app[SETTINGS_KEY].get()).pin_set:
        # The PIN was removed: nothing could unlock this tablet any more.
        claims = replace(claims, locked=False)
    request[CLAIMS_KEY] = claims
    if claims.locked and request.path not in _LOCK_PATHS:
        response = await _locked_response(request)
    else:
        response = await handler(request)
    if _may_renew(request, response) and auth.needs_renewal(claims):
        # Re-checked now, not at the top: a ``/lock`` (or a PIN sign-out)
        # may have revoked this cookie while the request was in flight.
        reason = auth.revocation(claims)
        if reason != REVOKED_SIGNED_OUT:
            await set_session_cookie(
                response,
                request,
                await auth.issue(
                    claims.keep,
                    locked=claims.locked or reason == REVOKED_LOCKED,
                    # Same session: same revocation entry, same PIN bucket.
                    session_id=claims.session_id,
                ),
                keep=claims.keep,
            )
    return response


#: The page routes whose loads renew the cookie (an allow-list): Home,
#: its Messages, Notes and Map tabs, a person page.
_RENEWING_PATH = re.compile(r"/(?:p/[^/]+|messages|notes|map)?")


def _may_renew(request: web.Request, response: web.StreamResponse) -> bool:
    """Only a ``200`` page load (``GET`` / ``HEAD`` of ``/``,
    ``/messages``, ``/notes``, ``/map`` or ``/p/{id}``) renews the cookie
    — an allow-list by path and status. Never a ``/fragment/`` fetch, an
    avatar, a map tile, a static file or a form ``POST``: such a response can land
    after a ``/lock`` (sent with the pre-lock cookie) and must not
    overwrite the locked cookie. Never a non-``200`` answer: an unlocked
    tablet's 404 (an unknown or hidden person, a tab that is off) or a
    tab's 303 (at night; Map also with nobody located). And never over a
    cookie the view set itself.

    A *locked* tablet is answered by the guard with the lock screen, a
    ``200`` on any of these paths (even ``/p/<unknown>`` or a tab that is
    off) — so it does renew, into a cookie that is still locked
    (:func:`_session_guard` re-issues ``locked``). Harmless: it only
    keeps the locked session alive."""
    return (
        request.method in ("GET", "HEAD")
        and response.status == 200
        and _RENEWING_PATH.fullmatch(request.path) is not None
        and COOKIE_NAME not in response.cookies
    )


async def _locked_response(request: web.Request) -> web.StreamResponse:
    """What a privacy-locked tablet gets instead of any content route."""
    if request.path.startswith(_FRAGMENT_PREFIX):
        return web.Response(
            text=LOCKED_FRAGMENT,
            content_type="text/html",
            charset="utf-8",
            headers={LOCKED_HEADER: "1"},
        )
    if request.method in ("GET", "HEAD") and not request.path.startswith(
        _ASSET_PREFIXES
    ):
        # 200, not a redirect: a reload keeps showing the lock screen.
        return await render_lock_screen(request)
    return web.Response(status=423, text="Locked")


def build_dashboard_app(
    *,
    settings_service: DashboardSettingsService,
    auth: DashboardAuth,
    displays: DashboardDisplayService,
    renderer: DashboardRenderer,
    household_name: HouseholdNameFn,
    household_tz: HouseholdTzFn,
    household_theme: HouseholdThemeFn,
    snapshots: DashboardSnapshots,
    tiles: DashboardTiles | None = None,
    extra_hosts: ExtraHosts = (),
    local_hostnames: frozenset[str] = frozenset(),
) -> web.Application:
    """A fresh dashboard application (one per :meth:`DashboardServer.start`).

    ``local_hostnames`` are this machine's own names (the caller resolves
    them once, e.g. from :func:`socket.gethostname`); with ``homeassistant``
    and ``extra_hosts`` they are the only single-label ``Host`` values the
    dashboard answers.

    Does no filesystem work — ``renderer`` (templates + static files) is
    built once by the caller at wiring time — so it is safe to call from
    the server's async start path.
    """
    app = web.Application(
        client_max_size=MAX_BODY_BYTES,
        middlewares=[
            _build_host_middleware(extra_hosts, local_hostnames),
            build_body_size_middleware(
                json_max_bytes=MAX_BODY_BYTES, media_max_bytes=MAX_BODY_BYTES
            ),
            _csrf_middleware,
            _session_guard,
        ],
    )
    app[SETTINGS_KEY] = settings_service
    app[AUTH_KEY] = auth
    app[DISPLAYS_KEY] = displays
    app[RENDERER_KEY] = renderer
    app[HOUSEHOLD_NAME_KEY] = household_name
    app[HOUSEHOLD_TZ_KEY] = household_tz
    app[HOUSEHOLD_THEME_KEY] = household_theme
    app[SNAPSHOTS_KEY] = snapshots
    app[AVATARS_KEY] = AvatarImages()
    app[EMOJI_KEY] = EmojiImages()
    if tiles is not None:
        app[TILES_KEY] = tiles
        app[MAP_PICTURES_KEY] = MapPictures(
            tiles=tiles,
            emoji=app[EMOJI_KEY],
            avatars=app[AVATARS_KEY],
            pictures=snapshots.avatar,
        )
    install_security_headers(app)
    # After the shared hook (which only ``setdefault``s), so these win.
    app.on_response_prepare.append(_dashboard_headers)
    register_routes(app)
    register_display_routes(app)
    return app
