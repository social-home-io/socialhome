"""HTTP hardening middleware (§25.7).

* :func:`build_body_size_middleware` — caps inbound bodies at
  ``json_max_bytes`` for ``application/json`` requests and
  ``media_max_bytes`` for everything else. The cap is best-effort —
  aiohttp's own ``client_max_size`` setting is the hard ceiling.
* :func:`build_cors_deny_middleware` — refuses any request whose
  ``Origin`` header is not in the operator's allowlist. The default
  policy is "deny everything", which is correct for a single-tenant
  household app — the frontend is served from the same origin.

Both middlewares slot into the global stack via :func:`create_app`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from aiohttp import web

log = logging.getLogger(__name__)


#: Default JSON body cap — 1 MiB (§25.7).
DEFAULT_JSON_MAX_BYTES: int = 1 * 1024 * 1024

#: Default media-upload body cap — 200 MiB matches the per-handler limit.
DEFAULT_MEDIA_MAX_BYTES: int = 200 * 1024 * 1024


# ─── Body-size middleware ────────────────────────────────────────────────


def build_body_size_middleware(
    *,
    json_max_bytes: int = DEFAULT_JSON_MAX_BYTES,
    media_max_bytes: int = DEFAULT_MEDIA_MAX_BYTES,
):
    """Build an aiohttp middleware that returns 413 for oversized bodies.

    The check is on ``Content-Length`` only — chunked uploads bypass
    it (aiohttp's own ``client_max_size`` covers those).  The
    federation webhook has its own per-route limit on top.
    """

    @web.middleware
    async def middleware(request: web.Request, handler):
        cl_header = request.headers.get("Content-Length")
        if cl_header is not None:
            try:
                cl = int(cl_header)
            except ValueError:
                return web.json_response(
                    {"error": "bad_content_length"},
                    status=400,
                )
            ctype = request.headers.get("Content-Type", "")
            cap = (
                json_max_bytes
                if ctype.startswith("application/json")
                else media_max_bytes
            )
            if cl > cap:
                return web.json_response(
                    {"error": "payload_too_large", "max_bytes": cap},
                    status=413,
                )
        return await handler(request)

    return middleware


# ─── CORS-deny middleware ────────────────────────────────────────────────


def build_cors_deny_middleware(
    *,
    allowed_origins: Iterable[str] = (),
):
    """Refuse any cross-origin request with an unallowed ``Origin``.

    A request without an ``Origin`` header (most native clients +
    same-origin browser fetches) passes through. When ``Origin`` is
    present and not in ``allowed_origins`` the request is rejected
    with 403.  CORS preflight (``OPTIONS``) requests are answered
    with the same allowlist — no permissive ``*`` ever.
    """
    allowlist: frozenset[str] = frozenset(allowed_origins or ())

    @web.middleware
    async def middleware(request: web.Request, handler):
        origin = request.headers.get("Origin")
        if origin is None:
            return await handler(request)
        if origin not in allowlist:
            log.debug("cors deny: blocked Origin=%r path=%s", origin, request.path)
            return web.json_response(
                {"error": "cors_denied"},
                status=403,
            )
        # Allowed — answer preflight directly.
        if request.method == "OPTIONS":
            return web.Response(
                status=204,
                headers={
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Credentials": "true",
                    "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": "Authorization, Content-Type",
                    "Access-Control-Max-Age": "600",
                },
            )
        # Regular request — annotate the response with the allow header.
        response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        return response

    return middleware


# ─── Security-headers middleware ────────────────────────────────────────

#: Headers injected on every response. Browsers ignore the ones they
#: don't understand (e.g. API-only clients), so there's no downside.
_SECURITY_HEADERS: dict[str, str] = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "X-XSS-Protection": "0",
}


def build_security_headers_middleware():
    """Inject standard security headers on every HTTP response.

    These defend against click-jacking (``X-Frame-Options``),
    MIME-sniffing (``X-Content-Type-Options``), and information
    leakage (``Referrer-Policy``). ``Strict-Transport-Security`` is
    intentionally omitted — the TLS terminator (HA Ingress or the
    operator's reverse proxy) should set it since only it knows
    whether HTTPS is enforced end-to-end.
    """

    @web.middleware
    async def middleware(request: web.Request, handler):
        response = await handler(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    return middleware
