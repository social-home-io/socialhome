"""The household dashboard's aiohttp application.

Served on its own port (:class:`~socialhome.dashboard.server.DashboardServer`)
for a wall tablet. It shares nothing with the main app's request
pipeline: no ``auth_middleware``, no bearer tokens and **no** HA ingress
headers — on this port ``X-Hass-Source`` / ``X-Remote-User-Name`` come
straight from the network and are spoofable. The only credential is the
signed session cookie (:mod:`socialhome.dashboard.auth`).

Middleware order: body-size cap → same-origin check on ``POST`` → the
session guard.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from urllib.parse import quote, urlsplit

from aiohttp import web

from ..csp import render_csp, style_hash
from ..hardening import build_body_size_middleware, install_security_headers
from ..i18n import Catalog
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
    "PUBLIC_PATHS",
    "build_dashboard_app",
    "build_dashboard_csp",
]

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
    "Referrer-Policy": "no-referrer",
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
        if request.method in ("GET", "HEAD") and not request.path.startswith(
            _FRAGMENT_PREFIX
        ):
            return redirect(
                "/login?next=" + quote(request.raw_path, safe=""), status=302
            )
        return web.Response(status=401, text="Unauthorized")
    auth.touch()
    request[CLAIMS_KEY] = claims
    response = await handler(request)
    if auth.needs_renewal(claims):
        set_session_cookie(
            response, request, await auth.issue(claims.keep), keep=claims.keep
        )
    return response


def build_dashboard_app(
    *,
    settings_service: DashboardSettingsService,
    auth: DashboardAuth,
    catalog: Catalog,
    household_name: HouseholdNameFn,
) -> web.Application:
    """A fresh dashboard application (one per :meth:`DashboardServer.start`)."""
    app = web.Application(
        client_max_size=MAX_BODY_BYTES,
        middlewares=[
            build_body_size_middleware(
                json_max_bytes=MAX_BODY_BYTES, media_max_bytes=MAX_BODY_BYTES
            ),
            _csrf_middleware,
            _session_guard,
        ],
    )
    app[SETTINGS_KEY] = settings_service
    app[AUTH_KEY] = auth
    app[RENDERER_KEY] = DashboardRenderer(catalog)
    app[HOUSEHOLD_NAME_KEY] = household_name
    install_security_headers(app)
    # After the shared hook (which only ``setdefault``s), so these win.
    app.on_response_prepare.append(_dashboard_headers)
    register_routes(app)
    return app
