"""The space seat gate — no ``/api/spaces/{id}/…`` route is an existence
oracle (owner decision 11).

Before this gate every space route checked access its own way, and most
of them looked the space up *first*: an unknown id answered ``404``
while a real space the caller had no seat in answered ``403`` ("admin or
owner required", "not a member of this space", …) — so any local user
could probe which space ids exist, and through the 403's wording learn
something about them.

The gate runs after auth and refuses a caller with **no seat** in the
path space — any ``space_members`` role, a follower included — with the
read gate's own answer, ``403 {"error": {"code": "FORBIDDEN", "detail":
"Not a space member."}}``, before the route runs. An unknown id has no
seats, so it gets exactly the same answer. The route's own checks (admin
only, member not follower, …) still run for a caller who has a seat.

Who passes without a seat:

* a **household admin** — the operator, who already lists every space
  (``GET /api/admin/spaces``) and may take a global space off a GFS;
* a **signed media URL** (``?exp=&sig=``) — a capability URL, the
  signature is the authorization (covers, icons, member pictures);
* the routes whose whole point is reaching a space you aren't in yet,
  each with its own uniform gate: the landing page (``GET
  /api/spaces/{id}``), asking to join (``POST …/join-requests``) and
  following (``POST`` / ``DELETE …/subscribe``).

Public paths (no auth context, e.g. the token-authorized calendar
``export.ics``) are not touched.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from aiohttp import web

from ..app_keys import space_repo_key
from ..auth import USER_KEY
from ..security import error_response
from ..services.space_member_guard import NOT_A_SPACE_MEMBER

log = logging.getLogger(__name__)

#: ``(method, route)`` pairs a caller with no seat may reach — each runs
#: its own gate that answers an unknown id the same as a hidden space.
SEATLESS_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/spaces/{id}"),
        ("POST", "/api/spaces/{id}/join-requests"),
        ("POST", "/api/spaces/{id}/subscribe"),
        ("DELETE", "/api/spaces/{id}/subscribe"),
    }
)

#: Route prefixes the gate covers, by the name of their space-id segment.
_SPACE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("/api/spaces/{id}", "id"),
    ("/api/spaces/{space_id}", "space_id"),
)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _space_id_of(request: web.Request) -> tuple[str, str] | None:
    """``(route, space id)`` when ``request`` hit a ``/api/spaces/{id}…``
    route, else ``None``."""
    route = request.match_info.route
    resource = route.resource
    if resource is None:
        return None
    canonical = resource.canonical
    for prefix, key in _SPACE_PREFIXES:
        if canonical == prefix or canonical.startswith(prefix + "/"):
            space_id = request.match_info.get(key)
            if space_id:
                return canonical, space_id
    return None


def build_space_seat_gate_middleware():
    """The aiohttp middleware: 403 ``Not a space member.`` for a caller
    with no seat in the path space (see the module docstring)."""

    @web.middleware
    async def space_seat_gate(
        request: web.Request, handler: Handler
    ) -> web.StreamResponse:
        hit = _space_id_of(request)
        if hit is None:
            return await handler(request)
        canonical, space_id = hit
        ctx = request.get(USER_KEY)
        if (
            ctx is None
            or ctx.is_admin
            or ctx.auth_method == "signed_url"
            or (request.method, canonical) in SEATLESS_ROUTES
        ):
            return await handler(request)
        seat = await request.app[space_repo_key].get_member(space_id, ctx.user_id)
        if seat is None:
            return error_response(403, "FORBIDDEN", NOT_A_SPACE_MEMBER)
        return await handler(request)

    return space_seat_gate
