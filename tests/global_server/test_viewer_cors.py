"""CORS on the anonymous public-highlight viewer routes, and nowhere else.

The pinned viewer is a static page on another origin (no GFS serves it),
so its requests to the GFS are cross-origin. Exactly the anonymous,
token-gated viewer routes answer them: ``Access-Control-Allow-Origin: *``
with no credentials, and the ``OPTIONS`` preflight of the JSON POSTs.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.global_server import public as gfs_public
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.server import create_gfs_app
from socialhome.global_server.viewer_cors import is_viewer_cors_path

ORIGIN = "https://social-home-io.github.io"

VIEWER_PATHS = [
    "/gfs/highlights/ice-servers",
    "/gfs/highlight_rtc/offer",
    "/gfs/highlight_rtc/session/abc",
    "/gfs/highlight_rtc/ice/viewer",
    "/gfs/highlight_rtc/relay/inst/s-1",
    # The public pages the viewer renders.
    "/gfs/pages/spaces/sp-1",
    "/gfs/pages/invite",
    "/gfs/pages/people/u-1",
]

OTHER_PATHS = [
    # Signed author routes of the same feature.
    "/gfs/highlight_rtc/answer",
    "/gfs/highlight_rtc/ice/author",
    "/gfs/highlight_rtc/relay-stream/r-1",
    "/gfs/highlights/s-1/publish",
    "/gfs/highlights/s-1/tokens",
    "/gfs/highlights/s-1/unpublish",
    "/gfs/highlight_tokens/t/revoke",
    # The GFS-served landing page and everything household / admin.
    "/highlight/inst/s-1/tok",
    "/gfs/info",
    "/gfs/register",
    # The pages themselves (a 302 or this server's HTML), the household
    # JSON they mirror, and anything past the page routes.
    "/spaces/sp-1",
    "/join/tok",
    "/moments/u-1",
    "/gfs/spaces/sp-1",
    "/gfs/moments/users/u-1",
    "/gfs/moments/users/u-1/picture",
    "/gfs/pages/spaces/",
    "/gfs/pages/spaces/sp-1/x",
    "/gfs/pages/people/u-1/picture",
    "/gfs/pages/invite/x",
    "/gfs/publish",
    "/gfs/rtc/offer",
    "/gfs/moment_rtc/offer",
    "/admin",
    "/admin/api/stats",
    "/",
    # Look-alikes.
    "/gfs/highlight_rtc/offer/x",
    "/gfs/highlight_rtc/session",
    "/gfs/highlights/ice-servers/x",
]


def test_is_viewer_cors_path_exactly_the_viewer_routes():
    for path in VIEWER_PATHS:
        assert is_viewer_cors_path(path), path
    for path in OTHER_PATHS:
        assert not is_viewer_cors_path(path), path


@pytest.fixture
async def client(tmp_dir):
    app = create_gfs_app(
        GfsConfig(
            host="127.0.0.1",
            port=0,
            base_url="http://gfs.test",
            data_dir=str(tmp_dir),
            instance_id="gfs-test",
        )
    )
    async with TestClient(TestServer(app)) as tc:
        tc._app = app
        yield tc


def _assert_cors(resp) -> None:
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"
    # Anonymous: never credentials, never an echoed origin.
    assert "Access-Control-Allow-Credentials" not in resp.headers


def _assert_no_cors(resp) -> None:
    assert not any(h.lower().startswith("access-control-") for h in resp.headers)


async def test_ice_servers_answers_any_origin(client):
    resp = await client.get("/gfs/highlights/ice-servers", headers={"Origin": ORIGIN})
    assert resp.status == 200
    _assert_cors(resp)


@pytest.mark.parametrize(
    "path",
    ["/gfs/highlight_rtc/offer", "/gfs/highlight_rtc/ice/viewer"],
)
async def test_preflight_for_the_json_posts(client, path):
    resp = await client.options(
        path,
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert resp.status == 204
    _assert_cors(resp)
    assert resp.headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"
    assert resp.headers["Access-Control-Allow-Headers"] == "content-type"
    assert resp.headers["Access-Control-Max-Age"] == "600"


async def test_uniform_unavailable_is_readable_cross_origin(client):
    """A refused offer (unknown token) still answers the viewer: the
    uniform 503 carries the header, so the page can say "not available"."""
    resp = await client.post(
        "/gfs/highlight_rtc/offer",
        json={
            "instance_id": "inst",
            "highlight_id": "s-1",
            "token": "nope",
            "sdp": "v=0",
            "vk": "x",
        },
        headers={"Origin": ORIGIN},
    )
    assert resp.status == 503
    _assert_cors(resp)


async def test_relay_and_session_carry_the_header(client):
    relay = await client.get(
        "/gfs/highlight_rtc/relay/inst/s-1?token=nope", headers={"Origin": ORIGIN}
    )
    assert relay.status == 503
    _assert_cors(relay)
    session = await client.get(
        "/gfs/highlight_rtc/session/unknown", headers={"Origin": ORIGIN}
    )
    assert session.status == 404
    _assert_cors(session)
    ice = await client.post(
        "/gfs/highlight_rtc/ice/viewer",
        json={"session_id": "unknown", "candidate": {}},
        headers={"Origin": ORIGIN},
    )
    assert ice.status == 404
    _assert_cors(ice)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/gfs/highlight_rtc/answer"),
        ("POST", "/gfs/highlight_rtc/ice/author"),
        ("POST", "/gfs/highlight_rtc/relay-stream/r-1"),
        ("POST", "/gfs/highlights/s-1/publish"),
        ("POST", "/gfs/highlight_tokens/t/revoke"),
        ("GET", "/highlight/inst/s-1/tok"),
        ("GET", "/gfs/info"),
        ("GET", "/"),
        ("GET", "/admin"),
    ],
)
async def test_no_cors_anywhere_else(client, method, path):
    resp = await client.request(method, path, headers={"Origin": ORIGIN})
    _assert_no_cors(resp)
    preflight = await client.options(
        path,
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"},
    )
    assert preflight.status != 204
    _assert_no_cors(preflight)


async def test_rate_limit_still_applies_and_is_readable(client):
    """CORS adds no way around the per-IP cap on the WS-amplifying offer;
    the 429 is readable cross-origin, so the page can say "too many"."""
    statuses = []
    for _ in range(gfs_public.PUBLIC_RTC_MAX_PER_MINUTE + 1):
        resp = await client.post(
            "/gfs/highlight_rtc/offer",
            json={"instance_id": "i", "highlight_id": "h", "token": "t", "sdp": "v"},
            headers={"Origin": ORIGIN},
        )
        statuses.append(resp.status)
        _assert_cors(resp)
    assert statuses[-1] == 429
