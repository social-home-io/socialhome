"""HTTP hardening middleware (§25.7).

* :func:`build_body_size_middleware` — caps inbound bodies at
  ``json_max_bytes`` for ``application/json`` requests and
  ``media_max_bytes`` for everything else, from ``Content-Length`` alone.
* :func:`read_body_capped` / :func:`read_part_capped` — stream a raw body
  or one multipart part under a per-route cap. Every whole-body read
  (``request.read()`` / ``json()`` / ``post()`` and, since aiohttp 3.13.3,
  ``BodyPartReader.read()``) is ceilinged by the app-wide
  ``client_max_size`` (:data:`DEFAULT_JSON_MAX_BYTES`); a route that takes
  a bigger body goes through these helpers instead of widening that
  ceiling for every route.
* :func:`build_cors_deny_middleware` — refuses any request whose
  ``Origin`` header is not in the operator's allowlist. The default
  policy is "deny everything", which is correct for a single-tenant
  household app — the frontend is served from the same origin.

Both middlewares slot into the global stack via :func:`create_app`.

* :func:`install_security_headers` — ``X-Frame-Options``,
  ``nosniff``, ``Referrer-Policy`` and ``Permissions-Policy`` on every
  response (an ``on_response_prepare`` hook, so streamed responses get
  them too).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

from .domain.errors import PayloadTooLargeError

log = logging.getLogger(__name__)


#: Default JSON body cap — 1 MiB (§25.7).
DEFAULT_JSON_MAX_BYTES: int = 1 * 1024 * 1024

#: Default media-upload body cap — 200 MiB matches the per-handler limit.
DEFAULT_MEDIA_MAX_BYTES: int = 200 * 1024 * 1024

#: Read granularity for the capped streaming readers. Large enough that a
#: legitimate body is a handful of chunks, small enough that an oversized
#: body is refused within one chunk of the cap.
_BODY_CHUNK_BYTES: int = 64 * 1024


# ─── Body-size middleware ────────────────────────────────────────────────


def build_body_size_middleware(
    *,
    json_max_bytes: int = DEFAULT_JSON_MAX_BYTES,
    media_max_bytes: int = DEFAULT_MEDIA_MAX_BYTES,
):
    """Build an aiohttp middleware that returns 413 for oversized bodies.

    The check is on ``Content-Length`` only — a chunked upload (no
    length declared) passes straight through. What then bounds it is how
    the route reads the body: every whole-body read (``request.read()`` /
    ``json()`` / ``post()`` and ``BodyPartReader.read()``) is capped by
    aiohttp's ``client_max_size`` (1 MiB, :data:`DEFAULT_JSON_MAX_BYTES`);
    a route that takes a bigger body must stream through
    :func:`read_body_capped` / :func:`read_part_capped`, which enforce the
    route's own cap chunk by chunk. The federation inbox has its own
    per-route limit on top.
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


# ─── Capped streaming readers ────────────────────────────────────────────


async def read_body_capped(request: web.BaseRequest, max_bytes: int) -> bytes:
    """Read the raw request body, refusing anything over *max_bytes*.

    Independent of the app-wide ``client_max_size``: ``request.read()``
    buffers the whole body and raises 413 past that ceiling (1 MiB by
    default), so a route that takes a bigger body (gallery items, backup
    archives) streams it here under its own cap instead. A declared
    ``Content-Length`` over the cap is refused before a byte is read; a
    chunked body (no length — what Home Assistant ingress forwards) is
    bounded while streaming. The ``Content-Length`` precheck is an early
    out only: aiohttp auto-decompresses a ``Content-Encoding: gzip``
    request body, so the declared length is the *compressed* size — the
    streaming cap on the decompressed bytes is the actual guarantee. Either
    way
    :class:`~socialhome.domain.errors.PayloadTooLargeError` is raised the
    moment the total crosses the cap — the rest of the body is never
    buffered.
    """
    declared = request.content_length
    if declared is not None and declared > max_bytes:
        raise PayloadTooLargeError(max_bytes)
    raw = bytearray()
    # ``StreamReader.read(n)`` returns only what is buffered, so the cap is
    # enforced by accumulating chunk by chunk and bailing the moment the
    # total crosses it.
    async for chunk in request.content.iter_chunked(_BODY_CHUNK_BYTES):
        raw += chunk
        if len(raw) > max_bytes:
            raise PayloadTooLargeError(max_bytes)
    return bytes(raw)


async def read_part_capped(part: aiohttp.BodyPartReader, max_bytes: int) -> bytes:
    """Read one multipart part, refusing anything over *max_bytes*.

    Since aiohttp 3.13.3 (aio-libs/aiohttp#11889, "Enforce client_max_size
    over entire multipart form") ``BodyPartReader.read()`` raises 413 once
    the part exceeds the app-wide ``client_max_size`` (1 MiB by default).
    ``read_chunk()`` is not covered by that ceiling, so this streams the
    part under the route's own cap and raises
    :class:`~socialhome.domain.errors.PayloadTooLargeError` the moment the
    total crosses it — the rest of the part is never buffered.
    """
    raw = bytearray()
    while True:
        chunk = await part.read_chunk(_BODY_CHUNK_BYTES)
        if not chunk:
            break
        raw += chunk
        if len(raw) > max_bytes:
            raise PayloadTooLargeError(max_bytes)
    return bytes(raw)


# ─── CORS-deny middleware ────────────────────────────────────────────────


def build_cors_deny_middleware(
    *,
    allowed_origins: Iterable[str] = (),
):
    """Refuse any cross-origin request with an unallowed ``Origin``.

    A request passes through when:

    * No ``Origin`` header — most native clients and some same-origin
      ``GET`` fetches.
    * ``Origin`` is **same-origin** with the request itself — i.e. the
      ``Origin`` host:port matches the request's host (or the
      ``X-Forwarded-Host`` if the operator sits behind a trusting
      reverse proxy). Modern browsers always set ``Origin`` on
      mutating same-origin fetches (POST/PUT/PATCH/DELETE) so a strict
      "no Origin" check would 403 every API call from the SPA when
      it's served by the same backend that handles the API — which is
      exactly how every prod path (haos via HA Ingress, ha behind a
      reverse proxy, standalone serving its own static bundle) works.
    * ``Origin`` is in the explicit ``allowed_origins`` allowlist —
      this stays the only knob for genuinely cross-origin SPAs.

    Any other ``Origin`` is rejected with 403 ``cors_denied``. CORS
    preflight (``OPTIONS``) requests are answered with the same
    allowlist — no permissive ``*`` ever.
    """
    allowlist: frozenset[str] = frozenset(allowed_origins or ())

    def _same_origin(origin: str, request: web.Request) -> bool:
        """``Origin``'s host:port matches what the client used to reach
        us. We trust ``X-Forwarded-Host`` when present (HA Ingress and
        most reverse proxies set it); otherwise fall back to ``Host``.
        Behind a misconfigured proxy that lets clients smuggle
        ``X-Forwarded-Host`` we'd over-trust — that's a deployment bug
        independent of CORS, and the operator who configured the proxy
        is responsible for not letting clients spoof it."""
        try:
            parsed = urlparse(origin)
        except ValueError:
            return False
        if not parsed.netloc:
            return False
        request_host = (
            request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or ""
        ).strip()
        if not request_host:
            return False
        return parsed.netloc.lower() == request_host.lower()

    @web.middleware
    async def middleware(request: web.Request, handler):
        origin = request.headers.get("Origin")
        if origin is None:
            return await handler(request)
        if _same_origin(origin, request):
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
    # ``SAMEORIGIN``, not ``DENY``: under ``haos`` the SPA is rendered by
    # HA's add-on ingress panel (home-assistant/frontend
    # ``src/panels/app/ha-panel-app.ts``) as ``<iframe src=
    # "/api/hassio_ingress/<token>/">`` — a same-origin frame of the HA
    # frontend, which ``DENY`` refuses to display at all. Supervisor and
    # Core's ingress proxies forward response headers unchanged. Other
    # origins still can't frame us (click-jacking).
    "X-Frame-Options": "SAMEORIGIN",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # Each feature is allowed for our own origin only, so cross-origin
    # embeds can never request it. In the HA ingress frame "our origin" is
    # HA's (the frame is same-origin with its parent), and a frame with no
    # ``allow`` attribute inherits the default ``'self'`` allowlist, so
    # calls work there. If a parent does narrow the policy for our frame,
    # the SPA detects it (``client/src/features/calls/embedPolicy.ts``).
    #
    # * ``geolocation`` — ``navigator.geolocation.getCurrentPosition()``
    #   for the location-share post composer + DM ShareLocationButton.
    # * ``camera`` / ``microphone`` — ``getUserMedia`` for voice/video
    #   calls (§26), voice notes, push-to-talk STT and the pairing QR
    #   scanner. ``()`` here made the browser reject every one of those
    #   with ``NotAllowedError`` before the user was even prompted.
    "Permissions-Policy": "camera=(self), microphone=(self), geolocation=(self)",
    "X-XSS-Protection": "0",
}


async def _apply_security_headers(
    _request: web.BaseRequest,
    response: web.StreamResponse,
) -> None:
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)


def install_security_headers(app: web.Application) -> None:
    """Add the standard security headers to every HTTP response of ``app``.

    These defend against click-jacking (``X-Frame-Options``),
    MIME-sniffing (``X-Content-Type-Options``), and information
    leakage (``Referrer-Policy``). ``Strict-Transport-Security`` is
    intentionally omitted — the TLS terminator (HA Ingress or the
    operator's reverse proxy) should set it since only it knows
    whether HTTPS is enforced end-to-end.

    Hooked on ``on_response_prepare``, not a middleware: a handler that
    ``prepare()``s its own ``StreamResponse`` (``/api/media/*``, app
    bundles) has already sent its headers when a middleware gets the
    response back. ``setdefault`` keeps a header the handler set
    explicitly.
    """
    app.on_response_prepare.append(_apply_security_headers)
