"""Fake Home Assistant Supervisor + Core for the haos ingress harness.

Test-only — never shipped, never imported by ``socialhome/``. Serves just
enough of the two HA surfaces a ``SH_MODE=haos`` backend talks to so it
boots and resolves ingress users:

Supervisor (``SUPERVISOR_URL``), every call must carry ``Authorization:
Bearer <SUPERVISOR_TOKEN>``: no token → 401, an unknown one → 403, like
the real Supervisor (``supervisor/api/middleware/security.py``
``token_validation``):

* ``GET /addons/self/info`` → ``{"result": "ok", "data": {...}}`` with
  ``hostname`` / ``ingress_port`` / ``network`` / ``host_network`` —
  supervisor ``main`` @ 143aa6e6, ``supervisor/api/apps.py``
  ``APIApps.info_data`` (the legacy ``/addons/{app}/info`` alias);
  envelope from ``supervisor/api/utils.py`` ``api_return_ok``.
* ``POST /discovery`` body ``{"service", "config"}`` →
  ``{"result": "ok", "data": {"uuid": ...}}`` —
  ``supervisor/api/discovery.py`` ``APIDiscovery.set_discovery``.

Core, reached through the Supervisor proxy at ``/core`` (Social Home's
``HaClient`` hard-codes ``http://supervisor/core``; ``run_backend.py``
re-points it here):

* ``GET /core/api/`` / ``/core/api/config`` / ``/core/api/states`` — REST
  (developers.home-assistant.io/docs/api/rest).
* ``/core/api/websocket`` — the Supervisor's WS proxy handshake
  (``supervisor/api/proxy.py`` ``APIProxy.websocket``): it sends
  ``{"type": "auth_required", "ha_version"}``, reads ``access_token``
  (the App's Supervisor token), answers ``auth_ok`` or
  ``{"type": "auth_invalid", "message": "Invalid access"}``.
  Commands answer with Core's result frame
  (core ``dev`` @ 5dacceaa, ``websocket_api/messages.py``
  ``result_message`` / ``error_message``); an unmocked command gets
  Core's ``unknown_command`` error, exactly like a real Core would for a
  typo'd command — so a wrong command name fails here too.
* ``config/auth/list`` rows are ``config/auth.py`` ``_user_info``:
  ``id, username, name, is_owner, is_active, local_only,
  system_generated, group_ids, credentials[{type}]``.

``GET /_log`` returns every call this fake served (the harness asserts on
it); ``GET /_users`` returns the user table (the ingress proxy reads it to
stamp ``X-Remote-User-*`` like the Supervisor does).

It also stands in for the Social Home Apps release (not an HA surface —
the backend's ``SH_APPS_CATALOG_URL`` points here so the harness can
install an app without the network): ``GET /_apps/catalog.json`` lists
one tiny app, ``ingress-probe``, in the shape ``AppCatalogEntry.from_dict``
reads (socialhome-apps ``scripts/build-catalog.mjs``), and
``GET /_apps/ingress-probe.tgz`` is its bundle — ``manifest.json`` and
one fully inlined ``index.html`` (the shape every app must ship: the
sandboxed frame's opaque origin can't load sub-resources). The probe asks
the host for ``app.context`` over the bridge and prints the answer
(with the launch context when "Play" opened it).

Usage: ``python fake_supervisor.py --port N --token T``
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import tarfile
import uuid

from aiohttp import WSMsgType, web

log = logging.getLogger("fake_supervisor")

HA_VERSION = "2026.10.0"

#: ``config/auth/list`` rows — HA's ``_user_info`` shape. alice is the
#: owner (the haos setup wizard provisions her as the Social Home admin);
#: bob + carol are ordinary HA users; the system user has no username,
#: exactly like HA's built-in "Supervisor" user.
USERS: list[dict] = [
    {
        "id": "a" * 32,
        "username": "alice",
        "name": "Alice Maple",
        "is_owner": True,
        "is_active": True,
        "local_only": False,
        "system_generated": False,
        "group_ids": ["system-admin"],
        "credentials": [{"type": "homeassistant"}],
    },
    {
        "id": "b" * 32,
        "username": "bob",
        "name": "Bob Builder",
        "is_owner": False,
        "is_active": True,
        "local_only": False,
        "system_generated": False,
        "group_ids": ["system-users"],
        "credentials": [{"type": "homeassistant"}],
    },
    {
        "id": "c" * 32,
        "username": "carol",
        "name": "Carol Chen",
        "is_owner": False,
        "is_active": True,
        "local_only": False,
        "system_generated": False,
        "group_ids": ["system-users"],
        "credentials": [{"type": "homeassistant"}],
    },
    {
        "id": "d" * 32,
        "username": None,
        "name": "Supervisor",
        "is_owner": False,
        "is_active": True,
        "local_only": False,
        "system_generated": True,
        "group_ids": ["system-admin"],
        "credentials": [],
    },
]

#: Every HA install has ``zone.home`` (created from the core config by
#: ``homeassistant/components/zone/__init__.py``); state = people inside.
#: Shape: REST ``GET /api/states`` state object
#: (developers.home-assistant.io/docs/api/rest).
STATES: list[dict] = [
    {
        "entity_id": "zone.home",
        "state": "0",
        "attributes": {
            "latitude": 52.3676,
            "longitude": 4.9041,
            "radius": 100,
            "passive": False,
            "persons": [],
            "editable": True,
            "icon": "mdi:home",
            "friendly_name": "Home",
        },
        "last_changed": "2026-10-01T00:00:00+00:00",
        "last_reported": "2026-10-01T00:00:00+00:00",
        "last_updated": "2026-10-01T00:00:00+00:00",
        "context": {
            "id": "01J0000000000000000000ZONE",
            "parent_id": None,
            "user_id": None,
        },
    },
]

#: WS commands this fake answers. Anything else → ``unknown_command``.
WS_RESULTS: dict[str, object] = {
    "config/auth/list": USERS,
    "get_states": STATES,
    "web_rtc/ice_servers": [],
    "config/device_registry/list": [],
    "config/entity_registry/list": [],
}

CALLS: list[dict] = []
TOKEN_KEY: web.AppKey[str] = web.AppKey("token", str)


def _record(kind: str, what: str, **extra: object) -> None:
    CALLS.append({"kind": kind, "what": what, **extra})


def _auth_status(request: web.Request) -> int:
    """200 when the Supervisor token matches, else the status the real
    Supervisor answers with (401 missing, 403 unknown)."""
    bearer = request.headers.get("Authorization", "")
    if not bearer:
        return 401
    return 200 if bearer == f"Bearer {request.app[TOKEN_KEY]}" else 403


def _deny(kind: str, what: str, status: int) -> web.HTTPException:
    _record(kind, what, status=status)
    return web.HTTPUnauthorized() if status == 401 else web.HTTPForbidden()


def _ok(data: object) -> web.Response:
    return web.json_response({"result": "ok", "data": data})


async def addon_info(request: web.Request) -> web.Response:
    if (status := _auth_status(request)) != 200:
        raise _deny("supervisor", "GET /addons/self/info", status)
    _record("supervisor", "GET /addons/self/info", status=200)
    return _ok(
        {
            "name": "Social Home",
            "slug": "local_socialhome",
            "hostname": "local-socialhome",
            "ingress": True,
            "ingress_port": 8099,
            "network": {"8100/tcp": 8100},
            "host_network": False,
            "options": {},
        }
    )


async def discovery(request: web.Request) -> web.Response:
    if (status := _auth_status(request)) != 200:
        raise _deny("supervisor", "POST /discovery", status)
    body = await request.json()
    if not isinstance(body, dict) or "service" not in body:
        _record("supervisor", "POST /discovery", status=400)
        return web.json_response(
            {"result": "error", "message": "missing service"}, status=400
        )
    _record("supervisor", "POST /discovery", status=200, service=body["service"])
    return _ok({"uuid": uuid.uuid4().hex})


async def core_rest(request: web.Request) -> web.Response:
    tail = request.match_info["tail"]
    what = f"{request.method} /api/{tail}"
    if (status := _auth_status(request)) != 200:
        raise _deny("core", what, status)
    if tail == "":
        _record("core", what, status=200)
        return web.json_response({"message": "API running."})
    if tail == "config":
        _record("core", what, status=200)
        return web.json_response(
            {
                "location_name": "Home",
                "latitude": 52.3676,
                "longitude": 4.9041,
                "time_zone": "Europe/Amsterdam",
                "currency": "EUR",
                "country": "NL",
                "language": "en",
                "unit_system": {"length": "km"},
                "version": HA_VERSION,
                "components": [],
            }
        )
    if tail == "states":
        _record("core", what, status=200)
        return web.json_response(STATES)
    if tail.startswith("states/"):
        entity_id = tail.removeprefix("states/")
        state = next((s for s in STATES if s["entity_id"] == entity_id), None)
        _record("core", what, status=200 if state else 404)
        if state is None:
            return web.json_response({"message": "Entity not found."}, status=404)
        return web.json_response(state)
    if request.method == "POST" and tail.startswith(("events/", "services/")):
        _record("core", what, status=200)
        return web.json_response([])
    # Unmocked: answer like HA does for an unknown route, and record it so
    # the report shows which Core endpoint Social Home reached for.
    _record("core", what, status=404, unmocked=True)
    return web.json_response({"message": "Not found"}, status=404)


async def core_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    await ws.send_json({"type": "auth_required", "ha_version": HA_VERSION})
    try:
        hello = await ws.receive_json(timeout=10)
    except TimeoutError, TypeError, ValueError:
        await ws.close()
        return ws
    if hello.get("access_token") != request.app[TOKEN_KEY]:
        _record("ws", "auth", status="auth_invalid")
        await ws.send_json({"type": "auth_invalid", "message": "Invalid access"})
        await ws.close()
        return ws
    await ws.send_json({"type": "auth_ok", "ha_version": HA_VERSION})
    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue
        data = json.loads(msg.data)
        cmd = data.get("type")
        if cmd in WS_RESULTS:
            _record("ws", cmd, status="ok")
            await ws.send_json(
                {
                    "id": data.get("id"),
                    "type": "result",
                    "success": True,
                    "result": WS_RESULTS[cmd],
                }
            )
        else:
            _record("ws", str(cmd), status="unknown_command", unmocked=True)
            await ws.send_json(
                {
                    "id": data.get("id"),
                    "type": "result",
                    "success": False,
                    "error": {"code": "unknown_command", "message": "Unknown command."},
                }
            )
    return ws


async def calls_view(request: web.Request) -> web.Response:
    return web.json_response(CALLS)


async def users_view(request: web.Request) -> web.Response:
    return web.json_response(USERS)


async def fallback(request: web.Request) -> web.Response:
    _record("supervisor", f"{request.method} {request.path}", status=404, unmocked=True)
    return web.json_response({"result": "error", "message": "not mocked"}, status=404)


PROBE_APP_ID = "ingress-probe"

_PROBE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Ingress probe</title>
<style>html,body{margin:0;height:100%;font:16px sans-serif}
body{display:flex;flex-direction:column;align-items:center;justify-content:center;background:#e8f0e8}</style>
</head><body><p id="out">waiting</p><button id="pick" type="button">Invite</button>
<p id="picked"></p><script>__PROBE_JS__</script></body></html>
"""

# The SDK's wire protocol (socialhome-apps packages/app-sdk): post
# {id, method} to the parent, read {id, ok, result} back.
# Opened by "Play" (``?session=``) the context's ``launch`` names the game
# — printed as ``launch <sid> invite`` (taken in now) or ``launch <sid>
# gone``.
_PROBE_JS = """window.addEventListener('message', function (e) {
  if (e.source !== window.parent || !e.data || e.data.id !== 1) return;
  var r = e.data.result || {};
  var l = r.launch || {};
  document.getElementById('out').textContent = !e.data.ok ? 'probe error'
    : 'probe ready ' + r.appId
      + (l.sessionId ? ' launch ' + l.sessionId + (l.invite ? ' invite' : ' gone') : '');
});
window.parent.postMessage({ id: 1, method: 'app.context' }, '*');
// "Invite" asks for Social Home's invite sheet (people.pick, D3) and prints
// ``picked <n> <session ids>`` or ``pick error <code>``.
window.addEventListener('message', function (e) {
  if (e.source !== window.parent || !e.data || e.data.id !== 2) return;
  var out = document.getElementById('picked');
  out.textContent = e.data.ok
    ? 'picked ' + e.data.result.length + ' '
      + e.data.result.map(function (p) { return p.session_id; }).join(',')
    : 'pick error ' + (e.data.error && e.data.error.code);
});
document.getElementById('pick').addEventListener('click', function () {
  window.parent.postMessage({ id: 2, method: 'people.pick', params: { max: 1 } }, '*');
});
"""


def _probe_bundle() -> bytes:
    """The ``ingress-probe`` bundle: a gzipped tar with the files at its root."""
    manifest = {
        "app_id": PROBE_APP_ID,
        "name": "Ingress probe",
        "version": "2026.1.1",
        "description": "Harness app: answers the host bridge.",
        "entry": "index.html",
        # Plays with people in this household only: the permissions the
        # Play launch needs (contacts, sessions, accept) — no federation.
        "capabilities": ["storage", "people"],
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in (
            ("manifest.json", json.dumps(manifest)),
            ("index.html", _PROBE_HTML.replace("__PROBE_JS__", _PROBE_JS)),
        ):
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


PROBE_BUNDLE = _probe_bundle()


async def apps_catalog(request: web.Request) -> web.Response:
    base = f"{request.scheme}://{request.host}"
    entry = {
        "app_id": PROBE_APP_ID,
        "name": "Ingress probe",
        "latest_version": "2026.1.1",
        "description": "Harness app: answers the host bridge.",
        "icon_url": None,
        # Plays with people in this household only: the permissions the
        # Play launch needs (contacts, sessions, accept) — no federation.
        "capabilities": ["storage", "people"],
        "bundle_url": f"{base}/_apps/{PROBE_APP_ID}.tgz",
        "bundle_sha256": hashlib.sha256(PROBE_BUNDLE).hexdigest(),
    }
    return web.json_response({"apps": [entry]})


async def apps_bundle(request: web.Request) -> web.Response:
    return web.Response(body=PROBE_BUNDLE, content_type="application/gzip")


def build_app(token: str) -> web.Application:
    app = web.Application()
    app[TOKEN_KEY] = token
    app.router.add_get("/_apps/catalog.json", apps_catalog)
    app.router.add_get(f"/_apps/{PROBE_APP_ID}.tgz", apps_bundle)
    app.router.add_get("/addons/self/info", addon_info)
    app.router.add_post("/discovery", discovery)
    app.router.add_get("/core/api/websocket", core_ws)
    app.router.add_route("*", "/core/api/{tail:.*}", core_rest)
    app.router.add_get("/_log", calls_view)
    app.router.add_get("/_users", users_view)
    app.router.add_route("*", "/{tail:.*}", fallback)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    web.run_app(
        build_app(args.token),
        host="127.0.0.1",
        port=args.port,
        print=lambda *_: print(f"fake supervisor on {args.port}", flush=True),
    )


if __name__ == "__main__":
    main()
