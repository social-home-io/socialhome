"""Tests for socialhome.platform.ha.client."""

from __future__ import annotations

import pytest
from aiohttp import web

from socialhome.platform.ha.client import HaClient, build_ha_client


# ─── Fake HA server ──────────────────────────────────────────────────────


@pytest.fixture
async def ha_server(aiohttp_server):
    """Mount a minimal in-process HA REST fake and return (server, captured)."""
    captured: dict = {"requests": []}

    async def _record(request: web.Request, body: dict | None = None) -> None:
        captured["requests"].append(
            {
                "method": request.method,
                "path": request.path,
                "query": dict(request.query),
                "headers": {
                    "Authorization": request.headers.get("Authorization"),
                    "X-Speech-Content": request.headers.get("X-Speech-Content"),
                },
                "body": body,
            }
        )

    async def api_root(request: web.Request) -> web.Response:
        await _record(request)
        return web.json_response({"message": "API running.", "username": "alice"})

    async def states_list(request: web.Request) -> web.Response:
        await _record(request)
        return web.json_response(
            [
                {"entity_id": "person.pascal", "attributes": {}},
                {"entity_id": "light.kitchen", "attributes": {}},
            ]
        )

    async def state_by_id(request: web.Request) -> web.Response:
        await _record(request)
        eid = request.match_info["entity_id"]
        if eid == "person.pascal":
            return web.json_response({"entity_id": eid, "attributes": {}})
        return web.json_response({}, status=404)

    async def config(request: web.Request) -> web.Response:
        await _record(request)
        return web.json_response({"location_name": "Home", "currency": "USD"})

    async def call_service(request: web.Request) -> web.Response:
        body = await request.json()
        await _record(request, body)
        # Respond with a service_response when asked
        if "return_response" in request.query:
            return web.json_response({"service_response": {"data": "ok"}})
        return web.json_response({})

    async def fire_event(request: web.Request) -> web.Response:
        body = await request.json()
        await _record(request, body)
        return web.json_response({"message": "Event fired"})

    async def stt(request: web.Request) -> web.Response:
        body = await request.read()
        await _record(request, {"byte_count": len(body)})
        return web.json_response({"result": "success", "text": "hi"})

    app = web.Application()
    app.router.add_get("/api/", api_root)
    app.router.add_get("/api/states", states_list)
    app.router.add_get(r"/api/states/{entity_id}", state_by_id)
    app.router.add_get("/api/config", config)
    app.router.add_post(r"/api/services/{domain}/{service}", call_service)
    app.router.add_post(r"/api/events/{event_type}", fire_event)
    app.router.add_post(r"/api/stt/{entity_id}", stt)
    server = await aiohttp_server(app)
    return server, captured


@pytest.fixture
async def session():
    import aiohttp

    async with aiohttp.ClientSession() as s:
        yield s


@pytest.fixture
def client(session, ha_server):
    server, _ = ha_server
    return HaClient(session, str(server.make_url("")).rstrip("/"), "secret-token")


# ─── Factory ─────────────────────────────────────────────────────────────


def test_build_ha_client_direct(session):
    c = build_ha_client(
        session,
        supervisor_token="",
        ha_url="http://ha.local:8123/",
        ha_token="t",
    )
    assert c.base_url == "http://ha.local:8123"


def test_build_ha_client_supervisor_overrides(session):
    c = build_ha_client(
        session,
        supervisor_token="sv-token",
        ha_url="http://ha.local:8123",
        ha_token="ignored",
    )
    assert c.base_url == "http://supervisor/core"


# ─── Call paths ──────────────────────────────────────────────────────────


async def test_verify_token_uses_supplied_token_header(client, ha_server):
    _, captured = ha_server
    data = await client.verify_token("user-supplied")
    assert data is not None and data["message"] == "API running."
    last = captured["requests"][-1]
    assert last["headers"]["Authorization"] == "Bearer user-supplied"


async def test_get_states_sends_bearer_token(client, ha_server):
    _, captured = ha_server
    states = await client.get_states()
    assert len(states) == 2
    assert captured["requests"][-1]["headers"]["Authorization"] == "Bearer secret-token"


async def test_get_state_handles_404(client):
    assert await client.get_state("person.missing") is None


async def test_get_config_success(client):
    cfg = await client.get_config()
    assert cfg is not None and cfg["location_name"] == "Home"


async def test_call_service_appends_return_response(client, ha_server):
    _, captured = ha_server
    result = await client.call_service(
        "ai_task",
        "generate_data",
        {"instructions": "x"},
        return_response=True,
    )
    assert result == {"service_response": {"data": "ok"}}
    assert captured["requests"][-1]["query"] == {"return_response": ""}


async def test_call_service_plain(client, ha_server):
    _, captured = ha_server
    result = await client.call_service(
        "notify",
        "mobile_app_pascal",
        {"title": "hi", "message": "m"},
    )
    assert result == {}
    assert captured["requests"][-1]["query"] == {}


async def test_fire_event_true_on_2xx(client):
    assert await client.fire_event("socialhome.post_created", {"id": "p1"}) is True


async def test_stream_stt_sends_metadata_header(client, ha_server):
    _, captured = ha_server

    async def _audio():
        yield b"frame1"
        yield b"frame2"

    result = await client.stream_stt(
        "stt.whisper",
        _audio(),
        language="en",
        sample_rate=16000,
        channels=1,
    )
    assert result == {"result": "success", "text": "hi"}
    last = captured["requests"][-1]
    hdr = last["headers"]["X-Speech-Content"]
    assert "format=wav" in hdr
    assert "sample_rate=16000" in hdr
    assert "language=en" in hdr
