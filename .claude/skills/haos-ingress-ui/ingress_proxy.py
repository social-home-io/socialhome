"""Home Assistant ingress look-alike reverse proxy (test-only).

A browser reaches a Home Assistant App's panel through two hops; this
proxy collapses both into one process:

1. **HA Core** — ``homeassistant/components/hassio/ingress.py``
   (core ``dev`` @ 5dacceaa), view ``/api/hassio_ingress/{token}/{path}``.
   ``_init_header`` copies the browser's headers minus
   ``INIT_HEADERS_FILTER`` (Content-Length/-Encoding, Transfer-Encoding,
   Accept-Encoding, Sec-WebSocket-*), then sets
   ``X-Hass-Source: core.ingress``,
   ``X-Ingress-Path: /api/hassio_ingress/<token>``, and
   ``X-Forwarded-For`` (appended), ``X-Forwarded-Host`` (kept if set,
   else the request host), ``X-Forwarded-Proto``. WebSockets are
   forwarded (``_handle_websocket``).
2. **Supervisor** — ``supervisor/api/ingress.py`` (supervisor ``main``
   @ 143aa6e6) ``_init_header``: resolves the ingress session's HA user
   and stamps ``X-Remote-User-Id`` / ``X-Remote-User-Name`` /
   ``X-Remote-User-Display-Name`` (the latter two only when the user has
   a username / name), drops any client-supplied ``X-Remote-User-*`` and
   ``X-Supervisor-Token`` / ``X-Hassio-Key``, appends the connecting
   peer to ``X-Forwarded-For``, and proxies to
   ``http://<app ip>:<ingress_port>/<path>`` (query string kept).

Not emulated: the Supervisor's ``ingress_session`` cookie (the browser
is signed in as whichever user ``/_proxy/user`` selected) and Core's
response compression.

Every request is logged. Anything the browser sends **outside** the
ingress prefix is a VIOLATION — the SPA escaped its ``<base href>`` —
and is answered 404, which is what HA Core would serve for an unknown
path under its own frontend.

Control endpoints (not part of HA):

* ``GET /_proxy/user?name=<username>|none`` — who the "Supervisor" signs
  in; ``none`` sends no ``X-Remote-User-*`` (the 401 → IngressAuthFailed
  path).
* ``GET /_proxy/log`` / ``GET /_proxy/reset`` — the request log.
* ``GET /frame`` — an HA-like page that embeds the panel in an iframe,
  the way the HA frontend mounts an ingress panel.

Usage: ``python ingress_proxy.py --port N --backend URL --token T
--users-url URL``
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import aiohttp
from aiohttp import WSMsgType, web

log = logging.getLogger("ingress_proxy")

#: Never forwarded upstream (Core's INIT_HEADERS_FILTER + hop-by-hop +
#: everything the Supervisor overwrites or strips).
_DROP_UPSTREAM = frozenset(
    h.lower()
    for h in (
        "Content-Length",
        "Content-Encoding",
        "Transfer-Encoding",
        "Accept-Encoding",
        "Sec-WebSocket-Extensions",
        "Sec-WebSocket-Protocol",
        "Sec-WebSocket-Version",
        "Sec-WebSocket-Key",
        "Connection",
        "Upgrade",
        "Keep-Alive",
        "Host",
        "X-Hass-Source",
        "X-Ingress-Path",
        "X-Remote-User-Id",
        "X-Remote-User-Name",
        "X-Remote-User-Display-Name",
        "X-Supervisor-Token",
        "X-Hassio-Key",
    )
)
#: Never copied back to the browser (Core's RESPONSE_HEADERS_FILTER +
#: hop-by-hop; aiohttp re-derives length / type from the body).
_DROP_DOWNSTREAM = frozenset(
    (
        "transfer-encoding",
        "content-length",
        "content-encoding",
        "connection",
        "keep-alive",
    )
)

STATE_KEY: web.AppKey[dict] = web.AppKey("state", dict)
SESSION_KEY: web.AppKey[aiohttp.ClientSession] = web.AppKey(
    "session", aiohttp.ClientSession
)


def upstream_headers(request: web.Request, state: dict) -> dict[str, str]:
    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _DROP_UPSTREAM
    }
    # HA Core hop.
    headers["X-Hass-Source"] = "core.ingress"
    headers["X-Ingress-Path"] = state["prefix"]
    peer = request.remote or "127.0.0.1"
    xff = request.headers.get("X-Forwarded-For")
    headers["X-Forwarded-For"] = f"{xff}, {peer}" if xff else peer
    headers["X-Forwarded-Host"] = request.headers.get("X-Forwarded-Host", request.host)
    headers["X-Forwarded-Proto"] = request.headers.get(
        "X-Forwarded-Proto", request.scheme
    )
    # Supervisor hop: appends Core's address (same host here).
    headers["X-Forwarded-For"] = f"{headers['X-Forwarded-For']}, 127.0.0.1"
    user = state["users"].get(state["user"])
    if user is not None:
        headers["X-Remote-User-Id"] = user["id"]
        if user.get("username") is not None:
            headers["X-Remote-User-Name"] = user["username"]
        if user.get("name") is not None:
            headers["X-Remote-User-Display-Name"] = user["name"]
    return headers


def _frame_page(prefix: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Home Assistant</title>
<link rel="icon" href="data:,"></head>
<body style="margin:0;display:flex;height:100vh">
<nav style="width:56px;background:#03a9f4"></nav>
<iframe id="app" title="Social Home" src="{prefix}/" style="flex:1;border:0"></iframe>
</body></html>"""


async def control(request: web.Request) -> web.Response:
    state = request.app[STATE_KEY]
    match request.path:
        case "/_proxy/user":
            name = request.query.get("name", "alice")
            if name != "none" and name not in state["users"]:
                return web.json_response({"error": f"unknown user {name}"}, status=400)
            state["user"] = name
            return web.json_response({"user": name})
        case "/_proxy/log":
            return web.json_response(state["log"])
        case "/_proxy/reset":
            state["log"].clear()
            return web.json_response({"ok": True})
        case "/frame":
            return web.Response(
                content_type="text/html", text=_frame_page(state["prefix"])
            )
    raise web.HTTPNotFound


async def _pipe_ws(
    src: web.WebSocketResponse | aiohttp.ClientWebSocketResponse,
    dst: web.WebSocketResponse | aiohttp.ClientWebSocketResponse,
) -> None:
    async for msg in src:
        if msg.type == WSMsgType.TEXT:
            await dst.send_str(msg.data)
        elif msg.type == WSMsgType.BINARY:
            await dst.send_bytes(msg.data)
    await dst.close()


async def handler(request: web.Request) -> web.StreamResponse:
    if request.path.startswith("/_proxy/") or request.path == "/frame":
        return await control(request)
    state = request.app[STATE_KEY]
    prefix = state["prefix"]
    is_ws = request.headers.get("Upgrade", "").lower() == "websocket"
    entry: dict = {"method": request.method, "path": request.path_qs, "ws": is_ws}
    state["log"].append(entry)
    if not (request.path == prefix or request.path.startswith(prefix + "/")):
        entry["violation"] = True
        entry["status"] = 404
        log.warning(
            "VIOLATION: %s %s outside %s", request.method, request.path_qs, prefix
        )
        return web.Response(
            status=404, text="404: Not Found (outside the ingress prefix)"
        )
    sub = request.path[len(prefix) :] or "/"
    url = state["backend"] + sub
    if request.query_string:
        url += "?" + request.query_string
    session = request.app[SESSION_KEY]
    headers = upstream_headers(request, state)
    if is_ws:
        # Upstream first, then accept the browser: when the browser sees
        # ``open`` the backend socket already exists, so a check that
        # waits for ``open`` never races the backend's registration. (HA
        # Core accepts the browser first; the order isn't observable to
        # the SPA beyond that timing.)
        try:
            client = await session.ws_connect(
                url.replace("http", "ws", 1), headers=headers
            )
        except aiohttp.WSServerHandshakeError as exc:
            entry["status"] = exc.status
            return web.Response(status=exc.status, text=exc.message)
        server = web.WebSocketResponse()
        await server.prepare(request)
        entry["status"] = 101
        async with client:
            await asyncio.gather(
                _pipe_ws(server, client),
                _pipe_ws(client, server),
                return_exceptions=True,
            )
        return server
    body = await request.read()
    async with session.request(
        request.method, url, headers=headers, data=body or None, allow_redirects=False
    ) as upstream:
        payload = await upstream.read()
        entry["status"] = upstream.status
        out = {
            k: v
            for k, v in upstream.headers.items()
            if k.lower() not in _DROP_DOWNSTREAM
        }
        return web.Response(status=upstream.status, body=payload, headers=out)


async def _load_users(users_url: str) -> dict[str, dict]:
    async with aiohttp.ClientSession() as s, s.get(users_url) as r:
        r.raise_for_status()
        rows = await r.json()
    return {u["username"]: u for u in rows if u.get("username")}


def build_app(backend: str, token: str, users: dict[str, dict]) -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app[STATE_KEY] = {
        "prefix": f"/api/hassio_ingress/{token}",
        "backend": backend.rstrip("/"),
        "user": "alice",
        "users": users,
        "log": [],
    }

    async def on_startup(app: web.Application) -> None:
        app[SESSION_KEY] = aiohttp.ClientSession(auto_decompress=True)

    async def on_cleanup(app: web.Application) -> None:
        await app[SESSION_KEY].close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_route("*", "/{tail:.*}", handler)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--backend", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--users-url", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    users = asyncio.run(_load_users(args.users_url))
    web.run_app(
        build_app(args.backend, args.token, users),
        host="127.0.0.1",
        port=args.port,
        print=lambda *_: print(f"ingress proxy on {args.port}", flush=True),
    )


if __name__ == "__main__":
    main()
