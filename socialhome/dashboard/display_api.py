"""The wall-panel JSON API on the dashboard port: ``/api/display/v1/…``.

A wall panel (e.g. an ESPHome display) is not a browser: it pairs with a
short code and then authenticates every request with
``Authorization: Bearer <token>``
(:class:`~socialhome.services.dashboard_display_service.DashboardDisplayService`).

* ``POST /api/display/v1/pair`` (public) — start pairing: ``201
  {pair_id, code, expires_in}``; throttled per client address
  (:meth:`DashboardAuth.begin_pair`);
* ``GET /api/display/v1/pair/{pair_id}`` (public) — ``{"status":
  "pending"}``, or once ``{"status": "paired", "token", "display_id"}``;
  ``404`` when unknown or expired;
* ``GET /api/display/v1/whoami`` (bearer) — ``{display_id, name}``.

The two credentials never cross: under this prefix the session guard
reads only the bearer token (a tablet cookie opens nothing here), and on
every other path only the cookie (a panel token opens no page). With no
cookie authority under the prefix there is nothing for a cross-site
request to ride on, so the same-origin ``POST`` check skips it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping

from aiohttp import web

from ..domain.errors import CodedError
from ..rate_limiter import client_bucket
from ..security import error_response
from .keys import AUTH_KEY, DISPLAY_KEY, DISPLAYS_KEY

log = logging.getLogger(__name__)

__all__ = [
    "DISPLAY_API_PREFIX",
    "PAIR_PATH",
    "bearer_token",
    "guard_display_request",
    "is_display_api",
    "is_public_display_path",
    "register_display_routes",
]

DISPLAY_API_PREFIX = "/api/display/v1/"
PAIR_PATH = "/api/display/v1/pair"
#: Panels report themselves in these headers (untrusted, length-capped).
FIRMWARE_HEADER = "X-Display-Firmware"
MODEL_HEADER = "X-Display-Model"

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def is_display_api(path: str) -> bool:
    return path.startswith(DISPLAY_API_PREFIX)


def is_public_display_path(path: str) -> bool:
    """The pairing endpoints — a panel has no token yet."""
    if path == PAIR_PATH:
        return True
    rest = path.removeprefix(PAIR_PATH + "/")
    # Exactly one non-empty segment — nothing nested below is public.
    return rest != path and bool(rest) and "/" not in rest


def bearer_token(headers: Mapping[str, str]) -> str | None:
    """The token of an ``Authorization: Bearer <token>`` header, else ``None``."""
    scheme, _, rest = headers.get("Authorization", "").strip().partition(" ")
    token = rest.strip()
    if scheme.lower() != "bearer" or not token or " " in token:
        return None
    return token


def _coded(exc: CodedError) -> web.Response:
    return error_response(
        exc.status,
        exc.code,
        exc.detail,
        extra={"params": exc.params} if exc.params else None,
    )


async def guard_display_request(
    request: web.Request, handler: Handler
) -> web.StreamResponse:
    """The session guard's branch for ``/api/display/v1/…``: the pairing
    endpoints are public, everything else needs a paired panel's token."""
    if is_public_display_path(request.path):
        return await handler(request)
    displays = request.app[DISPLAYS_KEY]
    display = await displays.authenticate(bearer_token(request.headers))
    if display is None:
        response = error_response(401, "UNAUTHORIZED", "A display token is required.")
        response.headers["WWW-Authenticate"] = "Bearer"
        return response
    request[DISPLAY_KEY] = display
    await displays.seen(
        display,
        firmware=request.headers.get(FIRMWARE_HEADER),
        model=request.headers.get(MODEL_HEADER),
    )
    return await handler(request)


class DisplayPairView(web.View):
    """``POST /api/display/v1/pair`` — a panel asks for a pairing code."""

    async def post(self) -> web.Response:
        # The socket peer only — never X-Forwarded-For, which anyone can set.
        wait = self.request.app[AUTH_KEY].begin_pair(client_bucket(self.request.remote))
        if wait:
            response = error_response(
                429, "RATE_LIMITED", "Too many pairing requests. Try again later."
            )
            response.headers["Retry-After"] = str(wait)
            return response
        raw = await self.request.read()
        body: object = {}
        if raw.strip():
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
        if not isinstance(body, dict):
            return error_response(400, "BAD_REQUEST", "Expected a JSON object.")
        try:
            pairing = await self.request.app[DISPLAYS_KEY].start_pairing(
                model=body.get("model"), firmware=body.get("firmware")
            )
        except CodedError as exc:
            return _coded(exc)
        return web.json_response(
            {
                "pair_id": pairing.pair_id,
                "code": pairing.code,
                "expires_in": pairing.expires_in,
            },
            status=201,
        )


class DisplayPairStatusView(web.View):
    """``GET /api/display/v1/pair/{pair_id}`` — the panel polls for its token."""

    async def get(self) -> web.Response:
        status = await self.request.app[DISPLAYS_KEY].poll(
            self.request.match_info["pair_id"]
        )
        if status is None:
            return error_response(404, "NOT_FOUND", "No such pairing.")
        if not status.paired:
            return web.json_response({"status": "pending"})
        return web.json_response(
            {
                "status": "paired",
                "token": status.token,
                "display_id": status.display_id,
            }
        )


class DisplayWhoamiView(web.View):
    """``GET /api/display/v1/whoami`` — which panel this token belongs to."""

    async def get(self) -> web.Response:
        display = self.request[DISPLAY_KEY]
        return web.json_response({"display_id": display.id, "name": display.name})


def register_display_routes(app: web.Application) -> None:
    app.router.add_view(PAIR_PATH, DisplayPairView)
    app.router.add_view(
        PAIR_PATH + "/{pair_id:[A-Za-z0-9_-]{1,64}}", DisplayPairStatusView
    )
    app.router.add_view(DISPLAY_API_PREFIX + "whoami", DisplayWhoamiView)
