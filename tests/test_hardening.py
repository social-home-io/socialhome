"""Tests for hardening middleware (§25.7)."""

from __future__ import annotations

import pytest
from aiohttp import web

from socialhome.hardening import (
    DEFAULT_JSON_MAX_BYTES,
    DEFAULT_MEDIA_MAX_BYTES,
    build_body_size_middleware,
    build_cors_deny_middleware,
)


# ─── Body-size middleware ────────────────────────────────────────────────


@pytest.fixture
async def body_client(aiohttp_client):
    """Tiny app with the body-size middleware + an echo handler."""

    async def echo(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application(
        middlewares=[
            build_body_size_middleware(json_max_bytes=1024, media_max_bytes=8192),
        ]
    )
    app.router.add_post("/", echo)
    return await aiohttp_client(app)


def test_default_caps_match_spec():
    assert DEFAULT_JSON_MAX_BYTES == 1 * 1024 * 1024
    assert DEFAULT_MEDIA_MAX_BYTES == 200 * 1024 * 1024


async def test_body_size_under_cap_passes(body_client):
    r = await body_client.post(
        "/",
        data=b'{"x":"y"}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status == 200


async def test_body_size_json_over_cap_413(body_client):
    big = b'{"x":"' + (b"y" * 2000) + b'"}'
    r = await body_client.post(
        "/",
        data=big,
        headers={"Content-Type": "application/json"},
    )
    assert r.status == 413


async def test_body_size_media_separate_cap(body_client):
    """Media uses the larger cap; 5 KiB octet-stream is fine."""
    r = await body_client.post(
        "/",
        data=b"x" * 5000,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert r.status == 200


async def test_bad_content_length_classified_as_400():
    """Defensive — exercises the int-parse branch directly.

    aiohttp's client validates Content-Length before sending so we
    can't trigger this path via TestClient. Call the middleware
    handler directly with a mocked request.
    """
    from aiohttp.test_utils import make_mocked_request

    mw = build_body_size_middleware(json_max_bytes=1024, media_max_bytes=1024)

    async def _h(_):
        return web.Response()

    req = make_mocked_request("POST", "/", headers={"Content-Length": "abc"})
    resp = await mw(req, _h)
    assert resp.status == 400


async def test_body_size_no_content_length_passes(body_client):
    """Chunked / no length → middleware lets it through (aiohttp guards it)."""
    r = await body_client.post("/")
    assert r.status == 200


# ─── CORS-deny middleware ────────────────────────────────────────────────


@pytest.fixture
async def cors_client(aiohttp_client):
    async def echo(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application(
        middlewares=[
            build_cors_deny_middleware(
                allowed_origins=("https://allowed.example",),
            ),
        ]
    )
    app.router.add_get("/", echo)
    app.router.add_post("/", echo)
    app.router.add_route("OPTIONS", "/", echo)
    return await aiohttp_client(app)


async def test_no_origin_passes(cors_client):
    """Same-origin / native-client requests carry no Origin and pass through."""
    r = await cors_client.get("/")
    assert r.status == 200


async def test_unallowed_origin_403(cors_client):
    r = await cors_client.get("/", headers={"Origin": "https://evil.example"})
    assert r.status == 403


async def test_allowed_origin_passes_with_acao(cors_client):
    r = await cors_client.get(
        "/",
        headers={"Origin": "https://allowed.example"},
    )
    assert r.status == 200
    assert r.headers["Access-Control-Allow-Origin"] == "https://allowed.example"
    assert r.headers["Access-Control-Allow-Credentials"] == "true"


async def test_preflight_returns_204_with_headers(cors_client):
    r = await cors_client.options(
        "/",
        headers={
            "Origin": "https://allowed.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert r.status == 204
    assert r.headers["Access-Control-Allow-Origin"] == "https://allowed.example"
    assert "POST" in r.headers["Access-Control-Allow-Methods"]


async def test_unallowed_preflight_403(cors_client):
    r = await cors_client.options(
        "/",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert r.status == 403


async def test_default_deny_all_when_allowlist_empty(aiohttp_client):
    async def echo(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application(
        middlewares=[
            build_cors_deny_middleware(allowed_origins=()),
        ]
    )
    app.router.add_get("/", echo)
    tc = await aiohttp_client(app)
    # No Origin: pass.
    assert (await tc.get("/")).status == 200
    # Any Origin: deny.
    r = await tc.get("/", headers={"Origin": "https://anything.example"})
    assert r.status == 403
