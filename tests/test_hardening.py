"""Tests for hardening middleware (§25.7)."""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp import web

from socialhome.domain.errors import PayloadTooLargeError
from socialhome.hardening import (
    DEFAULT_JSON_MAX_BYTES,
    DEFAULT_MEDIA_MAX_BYTES,
    build_body_size_middleware,
    build_cors_deny_middleware,
    install_security_headers,
    read_body_capped,
    read_part_capped,
)

# pytest-homeassistant-custom-component (a transitive dev dep when this
# repo's venv is shared with the ha-integration repo) installs a
# socket-blocking guard. The aiohttp TestClient needs a real port; CI
# doesn't install that plugin, so this fixture is a no-op there.
try:
    import pytest_socket  # noqa: F401

    @pytest.fixture(autouse=True)
    def _enable_sockets(socket_enabled):
        """Re-enable sockets if the HA pytest plugin disabled them."""

except ImportError:  # pragma: no cover - CI path
    pass


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


# ─── Capped streaming readers ────────────────────────────────────────────
#
# ``read_body_capped`` / ``read_part_capped`` stream the request instead
# of calling ``request.read()`` / ``BodyPartReader.read()``, so a route's
# own cap — not aiohttp's app-wide ``client_max_size`` (1 MiB default) —
# decides what it accepts.


@pytest.fixture
async def capped_client(aiohttp_client):
    """Default ``client_max_size`` (1 MiB) + handlers that stream through
    the helpers with a 10 MiB cap of their own."""

    cap = 10 * 1024 * 1024

    async def raw(request: web.Request) -> web.Response:
        try:
            data = await read_body_capped(request, cap)
        except PayloadTooLargeError as exc:
            return web.json_response({"max_mb": exc.params["max_mb"]}, status=413)
        return web.json_response({"n": len(data), "sha": data[:4].hex()})

    async def part(request: web.Request) -> web.Response:
        reader = await request.multipart()
        field = await reader.next()
        assert isinstance(field, aiohttp.BodyPartReader)
        try:
            data = await read_part_capped(field, cap)
        except PayloadTooLargeError as exc:
            return web.json_response({"max_mb": exc.params["max_mb"]}, status=413)
        return web.json_response({"n": len(data), "sha": data[:4].hex()})

    app = web.Application()
    app.router.add_post("/raw", raw)
    app.router.add_post("/part", part)
    return await aiohttp_client(app)


@pytest.fixture
async def tiny_cap_client(aiohttp_client):
    """Same handlers with a 4 KiB cap so the over-cap branches are cheap."""

    cap = 4096

    async def raw(request: web.Request) -> web.Response:
        if request.headers.get("X-Expect-Chunked"):
            # The chunked test proves the no-Content-Length shape.
            assert request.content_length is None
        try:
            data = await read_body_capped(request, cap)
        except PayloadTooLargeError as exc:
            return web.json_response({"max_mb": exc.params["max_mb"]}, status=413)
        return web.Response(text=str(len(data)))

    async def part(request: web.Request) -> web.Response:
        reader = await request.multipart()
        field = await reader.next()
        assert isinstance(field, aiohttp.BodyPartReader)
        try:
            data = await read_part_capped(field, cap)
        except PayloadTooLargeError as exc:
            return web.json_response({"max_mb": exc.params["max_mb"]}, status=413)
        return web.Response(text=str(len(data)))

    app = web.Application()
    app.router.add_post("/raw", raw)
    app.router.add_post("/part", part)
    return await aiohttp_client(app)


def _form(data: bytes) -> aiohttp.FormData:
    fd = aiohttp.FormData()
    fd.add_field(
        "file", data, filename="f.bin", content_type="application/octet-stream"
    )
    return fd


async def test_read_body_capped_under_cap_returns_exact_bytes(tiny_cap_client):
    body = bytes(range(256)) * 8  # 2 KiB
    r = await tiny_cap_client.post("/raw", data=body)
    assert r.status == 200
    assert await r.text() == str(len(body))


async def test_read_body_capped_refuses_declared_content_length_over_cap(
    tiny_cap_client,
):
    body = b"x" * 5000
    r = await tiny_cap_client.post("/raw", data=body)
    assert r.status == 413
    assert await r.json() == {"max_mb": 1}


async def test_read_body_capped_refuses_chunked_body_over_cap(tiny_cap_client):
    """No ``Content-Length`` (what HA ingress forwards) — the cap is
    enforced while streaming, not from a header the request lacks."""

    async def gen():
        for _ in range(10):
            yield b"y" * 1000

    r = await tiny_cap_client.post(
        "/raw", data=gen(), headers={"X-Expect-Chunked": "1"}
    )
    assert r.status == 413
    assert await r.json() == {"max_mb": 1}


async def test_read_part_capped_under_cap_returns_bytes(tiny_cap_client):
    body = b"z" * 3000
    r = await tiny_cap_client.post("/part", data=_form(body))
    assert r.status == 200
    assert await r.text() == str(len(body))


async def test_read_part_capped_refuses_part_over_cap(tiny_cap_client):
    r = await tiny_cap_client.post("/part", data=_form(b"z" * 5000))
    assert r.status == 413
    assert await r.json() == {"max_mb": 1}


async def test_read_part_capped_reads_past_aiohttp_default_client_max_size(
    capped_client,
):
    """aiohttp >= 3.13.3 (aio-libs/aiohttp#11889) makes
    ``BodyPartReader.read()`` raise 413 once a part exceeds the app's
    ``client_max_size`` (1 MiB by default). The helper streams the part in
    chunks, so a 3 MiB part under the route's own 10 MiB cap is accepted
    by an app that never widened ``client_max_size``."""
    body = b"\x01\x02\x03\x04" + b"m" * (3 * 1024 * 1024)
    assert len(body) > DEFAULT_JSON_MAX_BYTES
    r = await capped_client.post("/part", data=_form(body))
    assert r.status == 200
    assert await r.json() == {"n": len(body), "sha": "01020304"}


async def test_read_body_capped_reads_past_aiohttp_default_client_max_size(
    capped_client,
):
    """``request.read()`` has always honoured ``client_max_size``; the
    streaming helper does not, so a 3 MiB raw body passes a 10 MiB cap."""
    body = b"\x0a\x0b\x0c\x0d" + b"r" * (3 * 1024 * 1024)
    assert len(body) > DEFAULT_JSON_MAX_BYTES
    r = await capped_client.post("/raw", data=body)
    assert r.status == 200
    assert await r.json() == {"n": len(body), "sha": "0a0b0c0d"}


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
    # Any cross-origin: deny.
    r = await tc.get("/", headers={"Origin": "https://anything.example"})
    assert r.status == 403


# ─── Same-origin allow path (the haos / ha-prod / standalone-prod case) ──


async def test_same_origin_post_passes_without_allowlist(aiohttp_client):
    """Modern browsers send Origin on every same-origin POST. The
    middleware must let that through without an env-var allowlist —
    otherwise haos production (SPA at HA-host serving the API at
    HA-host) and every other "served by the backend" path 403s every
    mutation. Same-origin is detected by comparing the Origin's
    netloc to the request's Host."""

    async def echo(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application(
        middlewares=[build_cors_deny_middleware(allowed_origins=())],
    )
    app.router.add_post("/", echo)
    tc = await aiohttp_client(app)
    # The TestClient hits 127.0.0.1:<port>, so synthesise a matching
    # Origin to mimic what a same-origin browser fetch would send.
    host = f"{tc.host}:{tc.port}"
    r = await tc.post("/", headers={"Origin": f"http://{host}"})
    assert r.status == 200


async def test_x_forwarded_host_drives_same_origin_detection(aiohttp_client):
    """Behind HA Ingress / a reverse proxy, the request's Host header
    is often the proxy's internal name while the browser's Origin
    matches the public hostname forwarded as X-Forwarded-Host."""

    async def echo(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application(
        middlewares=[build_cors_deny_middleware(allowed_origins=())],
    )
    app.router.add_post("/", echo)
    tc = await aiohttp_client(app)
    r = await tc.post(
        "/",
        headers={
            "Origin": "https://ha.example",
            "X-Forwarded-Host": "ha.example",
        },
    )
    assert r.status == 200


async def test_same_origin_match_is_case_insensitive(aiohttp_client):
    async def echo(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application(
        middlewares=[build_cors_deny_middleware(allowed_origins=())],
    )
    app.router.add_post("/", echo)
    tc = await aiohttp_client(app)
    r = await tc.post(
        "/",
        headers={
            "Origin": "https://HA.Example",
            "X-Forwarded-Host": "ha.example",
        },
    )
    assert r.status == 200


async def test_genuine_cross_origin_still_denied_when_origin_host_differs(
    aiohttp_client,
):
    async def echo(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application(
        middlewares=[build_cors_deny_middleware(allowed_origins=())],
    )
    app.router.add_post("/", echo)
    tc = await aiohttp_client(app)
    # Attacker's Origin doesn't match Host or X-Forwarded-Host.
    r = await tc.post(
        "/",
        headers={
            "Origin": "https://evil.example",
            "X-Forwarded-Host": "ha.example",
        },
    )
    assert r.status == 403


# ─── Security-headers middleware ─────────────────────────────────────────


async def test_permissions_policy_allows_same_origin_camera_and_microphone(
    aiohttp_client,
):
    """Calls, voice notes, STT and the QR scanner all call getUserMedia
    from the same-origin SPA — ``camera=()`` / ``microphone=()`` made the
    browser reject every one of them with NotAllowedError. Same-origin is
    allowed; cross-origin embeds stay denied."""

    async def echo(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    install_security_headers(app)
    app.router.add_get("/", echo)
    tc = await aiohttp_client(app)
    r = await tc.get("/")
    policy = {
        part.split("=", 1)[0].strip(): part.split("=", 1)[1].strip()
        for part in r.headers["Permissions-Policy"].split(",")
    }
    assert policy["camera"] == "(self)"
    assert policy["microphone"] == "(self)"
    assert policy["geolocation"] == "(self)"
    # Same-origin framing only — see the next test.
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN"


async def test_security_headers_let_the_ha_ingress_panel_frame_the_spa(aiohttp_client):
    """Under ``haos`` HA's add-on ingress panel renders the SPA as a
    same-origin ``<iframe src="/api/hassio_ingress/<token>/">``. ``DENY``
    made the browser refuse to display that frame at all; ``SAMEORIGIN``
    allows it while still refusing every other origin."""

    async def echo(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    install_security_headers(app)
    app.router.add_get("/", echo)
    tc = await aiohttp_client(app)
    r = await tc.get("/")
    assert r.headers["X-Frame-Options"] != "DENY"
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN"


async def test_security_headers_reach_streamed_responses(aiohttp_client):
    """A handler that ``prepare()``s its own ``StreamResponse`` (media,
    app bundles) sends its headers before any middleware sees the
    response again — the hardening headers must be added at prepare
    time, not after the handler returns."""

    async def stream(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"X-Frame-Options": "DENY"})
        await resp.prepare(request)
        await resp.write(b"chunk")
        await resp.write_eof()
        return resp

    app = web.Application()
    install_security_headers(app)
    app.router.add_get("/", stream)
    tc = await aiohttp_client(app)
    r = await tc.get("/")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "Permissions-Policy" in r.headers
    # A header the handler set explicitly wins.
    assert r.headers["X-Frame-Options"] == "DENY"
