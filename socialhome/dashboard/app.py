"""The household dashboard's aiohttp application.

Served on its own port (:class:`~socialhome.dashboard.server.DashboardServer`)
for a wall tablet. It shares nothing with the main app's request
pipeline: no ``auth_middleware``, no bearer tokens and **no** HA ingress
headers — on this port ``X-Hass-Source`` / ``X-Remote-User-Name`` come
straight from the network and are spoofable. The only credential is the
signed session cookie (:mod:`socialhome.dashboard.auth`).

Middleware order: ``Host`` allow-list (DNS rebinding) → body-size cap →
same-origin check on ``POST`` → the session guard.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from urllib.parse import quote, urlsplit

from aiohttp import web

from ..csp import render_csp, style_hash
from ..hardening import build_body_size_middleware, install_security_headers
from ..services.dashboard_settings_service import DashboardSettingsService
from .auth import COOKIE_NAME, DashboardAuth
from .keys import (
    AUTH_KEY,
    CLAIMS_KEY,
    HOUSEHOLD_NAME_KEY,
    RENDERER_KEY,
    SETTINGS_KEY,
    HouseholdNameFn,
)
from .render import DashboardRenderer
from .views import redirect, register_routes, set_session_cookie

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

#: Same-origin only; no third-party host of any kind (no web fonts, no
#: CDNs). Inline ``<style>`` blocks are admitted per page by hash
#: (:func:`build_dashboard_csp`); there is no ``'unsafe-inline'``.
DASHBOARD_CSP_DIRECTIVES: Mapping[str, tuple[str, ...]] = {
    "default-src": ("'self'",),
    "script-src": ("'self'",),
    "style-src": ("'self'",),
    "img-src": ("'self'", "data:"),
    "connect-src": ("'self'",),
    "font-src": ("'self'",),
    "object-src": ("'none'",),
    "base-uri": ("'none'",),
    "form-action": ("'self'",),
    "frame-ancestors": ("'none'",),
}

_PAGE_HEADERS: dict[str, str] = {
    "X-Frame-Options": "DENY",
    # Not ``no-referrer``: under it browsers send ``Origin: null`` on our
    # own form POSTs, which the same-origin check must refuse. Nothing
    # leaves the origin either way — the dashboard links nowhere else.
    "Referrer-Policy": "same-origin",
    # Wake lock is for keeping the wall tablet's screen on (later PR).
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), screen-wake-lock=(self)"
    ),
}


def build_dashboard_csp(inline_styles: Iterable[str] = ()) -> str:
    """The dashboard policy, admitting exactly ``inline_styles`` by hash."""
    hashes = tuple(dict.fromkeys(style_hash(css) for css in inline_styles))
    directives = dict(DASHBOARD_CSP_DIRECTIVES)
    directives["style-src"] = (*directives["style-src"], *hashes)
    return render_csp(directives)


_DEFAULT_CSP = build_dashboard_csp()


async def _dashboard_headers(
    request: web.BaseRequest, response: web.StreamResponse
) -> None:
    for name, value in _PAGE_HEADERS.items():
        response.headers[name] = value
    response.headers.setdefault("Content-Security-Policy", _DEFAULT_CSP)
    if not request.path.startswith(_STATIC_PREFIX):
        # Pages, redirects (which may carry Set-Cookie) and errors are
        # per-session — never cache them; only the stylesheet may be.
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
    if request.method == "POST" and not _same_origin(request):
        log.info("household dashboard: refused cross-origin POST to %s", request.path)
        return web.Response(status=403, text="Forbidden")
    return await handler(request)


@web.middleware
async def _session_guard(request: web.Request, handler) -> web.StreamResponse:
    if _is_public(request.path):
        return await handler(request)
    auth = request.app[AUTH_KEY]
    claims = await auth.verify(request.cookies.get(COOKIE_NAME))
    if claims is None:
        # Only a page view (GET) is sent to the login form; HEAD, POST and
        # ``/fragment/`` requests (fetched by page JS) get a plain 401.
        if request.method == "GET" and not request.path.startswith(_FRAGMENT_PREFIX):
            return redirect(
                "/login?next=" + quote(request.raw_path, safe=""), status=302
            )
        return web.Response(status=401, text="Unauthorized")
    auth.touch()
    request[CLAIMS_KEY] = claims
    response = await handler(request)
    if auth.needs_renewal(claims):
        await set_session_cookie(
            response, request, await auth.issue(claims.keep), keep=claims.keep
        )
    return response


def build_dashboard_app(
    *,
    settings_service: DashboardSettingsService,
    auth: DashboardAuth,
    renderer: DashboardRenderer,
    household_name: HouseholdNameFn,
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
    app[RENDERER_KEY] = renderer
    app[HOUSEHOLD_NAME_KEY] = household_name
    install_security_headers(app)
    # After the shared hook (which only ``setdefault``s), so these win.
    app.on_response_prepare.append(_dashboard_headers)
    register_routes(app)
    return app
