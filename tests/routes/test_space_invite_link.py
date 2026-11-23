"""Integration tests for ``POST /api/spaces/invite-link``.

The invite QR carries a GFS's short ``…/join/{token}`` link; the household
resolves it to the invite code by asking that GFS. A fake GFS runs on
loopback, which only a GFS this household is connected to may be (a
connection is an admin's choice) — every other loopback / private / http
link is refused before any request leaves. The https-to-a-public-host path
is covered with a session double in
``tests/services/test_invite_link_service.py``.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from socialhome.app_keys import gfs_connection_repo_key
from socialhome.domain.federation import GfsConnection

pytestmark = pytest.mark.integration

CODE = "socialhome://invite#eyJ0b2tlbiI6ICJhYmMifQ"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def fake_gfs():
    """A GFS answering ``POST /gfs/pages/invite`` like the real one."""
    seen: list[dict] = []

    async def pages_invite(request: web.Request) -> web.Response:
        body = await request.json()
        seen.append(body)
        if body.get("token") == "live-token":
            return web.json_response(
                {"server_name": "G", "space": {}, "invite_code": CODE, "qr": ""}
            )
        if body.get("token") == "moved":
            raise web.HTTPFound("http://127.0.0.1:1/elsewhere")
        if body.get("token") == "garbage":
            return web.json_response({"invite_code": "https://evil.example/x"})
        return web.json_response({"server_name": "G"}, status=404)

    app = web.Application()
    app.router.add_post("/gfs/pages/invite", pages_invite)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    server.seen = seen  # type: ignore[attr-defined]
    try:
        yield server
    finally:
        await server.close()


async def _connect(client, url: str) -> None:
    await client.app[gfs_connection_repo_key].save(
        GfsConnection(
            id="gfs-1",
            gfs_instance_id="gfs-1",
            display_name="G",
            public_key="aa" * 32,
            inbox_url=url,
            status="active",
            paired_at="2026-11-01T00:00:00+00:00",
        )
    )


def _base(server: TestServer) -> str:
    return str(server.make_url("")).rstrip("/")


async def _resolve(client, url):
    return await client.post(
        "/api/spaces/invite-link", json={"url": url}, headers=_auth(client._tok)
    )


async def test_connected_gfs_link_resolves_to_its_code(client, fake_gfs):
    await _connect(client, _base(fake_gfs))
    r = await _resolve(client, f"{_base(fake_gfs)}/join/live-token")
    assert r.status == 200
    assert await r.json() == {"invite_code": CODE}
    assert fake_gfs.seen == [{"token": "live-token"}]


async def test_trailing_slash_on_the_stored_url_still_matches(client, fake_gfs):
    await _connect(client, _base(fake_gfs) + "/")
    r = await _resolve(client, f"{_base(fake_gfs)}/join/live-token")
    assert r.status == 200


async def test_dead_invite_is_404(client, fake_gfs):
    await _connect(client, _base(fake_gfs))
    r = await _resolve(client, f"{_base(fake_gfs)}/join/revoked")
    assert r.status == 404
    assert (await r.json())["error"]["code"] == "INVITE_LINK_EXPIRED"


async def test_redirect_is_not_followed(client, fake_gfs):
    await _connect(client, _base(fake_gfs))
    r = await _resolve(client, f"{_base(fake_gfs)}/join/moved")
    assert r.status == 502
    assert (await r.json())["error"]["code"] == "INVITE_LINK_UNREACHABLE"
    assert len(fake_gfs.seen) == 1


async def test_an_answer_that_is_not_an_invite_code_is_not_passed_on(client, fake_gfs):
    await _connect(client, _base(fake_gfs))
    r = await _resolve(client, f"{_base(fake_gfs)}/join/garbage")
    assert r.status == 502


async def test_loopback_gfs_we_are_not_connected_to_is_refused(client, fake_gfs):
    # Same server, but not a connection of this household: refused before
    # any request (http, and loopback).
    r = await _resolve(client, f"{_base(fake_gfs)}/join/live-token")
    assert r.status == 422
    assert (await r.json())["error"]["code"] == "INVITE_LINK_REFUSED"
    assert fake_gfs.seen == []


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/join/tok",
        "https://127.0.0.1:8443/join/tok",
        "https://localhost/join/tok",
        "https://10.0.0.7/join/tok",
        "https://192.168.1.1/join/tok",
        "https://169.254.169.254/join/tok",
        "https://100.64.0.1/join/tok",
        "https://[::1]/join/tok",
        "https://[fe80::1]/join/tok",
        "https://[::ffff:10.0.0.1]/join/tok",
        "https://0x7f000001/join/tok",
        "https://2130706433/join/tok",
        "http://gfs.example.org/join/tok",
        "ftp://gfs.example.org/join/tok",
        "https://user:pw@gfs.example.org/join/tok",
        "https://gfs.example.org/join/tok?x=1",
        "https://gfs.example.org/join/tok#frag",
        "https://gfs.example.org/spaces/tok",
        "https://gfs.example.org/join/",
        "https://gfs.example.org/join/..",
        "https://gfs.example.org/join/a b",
        "https://gfs.example.org\\@evil/join/tok",
        "socialhome://invite#abc",
        "",
        None,
        42,
    ],
)
async def test_refused_links(client, url):
    r = await _resolve(client, url)
    assert r.status == 422, url
    assert (await r.json())["error"]["code"] == "INVITE_LINK_REFUSED"


async def test_needs_a_signed_in_member(client):
    r = await client.post(
        "/api/spaces/invite-link", json={"url": "https://gfs.example.org/join/t"}
    )
    assert r.status == 401


async def test_rate_limited_per_user(client):
    codes = [
        (await _resolve(client, "https://127.0.0.1/join/tok")).status for _ in range(11)
    ]
    assert codes[:10] == [422] * 10
    assert codes[10] == 429
