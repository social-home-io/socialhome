"""CORS for the anonymous public-highlight viewer routes (§highlights_public).

A public highlight link opens the PINNED viewer: a static page on an
origin no GFS runs (see ``socialhome/pinned_viewer.py``). Its requests to
this GFS are therefore cross-origin, and only these routes answer them:

* ``GET  /gfs/highlights/ice-servers``
* ``POST /gfs/highlight_rtc/offer``
* ``GET  /gfs/highlight_rtc/session/{session_id}``
* ``POST /gfs/highlight_rtc/ice/viewer``
* ``GET  /gfs/highlight_rtc/relay/{instance_id}/{highlight_id}``

They are anonymous and token-gated already: anyone could call them from a
script outside a browser. ``Access-Control-Allow-Origin: *`` only lets a
browser page read the answers too. Never credentials (no
``Allow-Credentials``, and ``*`` forbids them anyway), never on a signed
author route, the GFS-served landing page, a household route or the admin
portal. The per-IP rate limits run as before; the header rides on their
``429`` too, so the page can tell the viewer to wait.
"""

from __future__ import annotations

from aiohttp import web

#: Exact viewer paths.
_EXACT: frozenset[str] = frozenset(
    {
        "/gfs/highlights/ice-servers",
        "/gfs/highlight_rtc/offer",
        "/gfs/highlight_rtc/ice/viewer",
    }
)

#: Viewer routes with one trailing path parameter (session poll) or two
#: (relay GET). ``/relay/`` — with the slash — is not ``/relay-stream/``.
_PREFIXES: tuple[tuple[str, int], ...] = (
    ("/gfs/highlight_rtc/session/", 1),
    ("/gfs/highlight_rtc/relay/", 2),
)

#: The preflight answer: the JSON POSTs need ``content-type``; nothing else.
PREFLIGHT_HEADERS: dict[str, str] = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "content-type",
    "Access-Control-Max-Age": "600",
}


def is_viewer_cors_path(path: str) -> bool:
    """Whether ``path`` is one of the anonymous viewer routes above."""
    if path in _EXACT:
        return True
    for prefix, segments in _PREFIXES:
        if path.startswith(prefix):
            rest = path[len(prefix) :].split("/")
            return len(rest) == segments and all(rest)
    return False


@web.middleware
async def _viewer_preflight(request: web.Request, handler):
    """Answer the ``OPTIONS`` preflight of a viewer route; pass the rest."""
    if request.method == "OPTIONS" and is_viewer_cors_path(request.rel_url.path):
        return web.Response(status=204, headers=PREFLIGHT_HEADERS)
    return await handler(request)


async def _allow_any_origin(
    request: web.BaseRequest,
    response: web.StreamResponse,
) -> None:
    if is_viewer_cors_path(request.rel_url.path):
        response.headers["Access-Control-Allow-Origin"] = "*"


def install_viewer_cors(app: web.Application) -> None:
    """Hook the viewer CORS onto ``app``.

    The header is added on ``on_response_prepare``, not in a middleware:
    the relay GET ``prepare()``s its own streamed response, and a
    rate-limit ``429`` or an ``HTTPException`` never reaches a middleware's
    return path with headers still open. The preflight is the one thing a
    middleware must answer (no view has an ``options`` method).
    """
    app.middlewares.insert(0, _viewer_preflight)
    app.on_response_prepare.append(_allow_any_origin)
