"""Tests for :mod:`socialhome.peer_http` — how we POST to a
URL another household (or a connection server) gave us, without letting a
3xx steer the signed envelope somewhere that URL never pointed."""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from socialhome.peer_http import (
    post_to_peer,
    safe_redirect_target,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent

# ─── safe_redirect_target (pure) ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("origin", "location", "expected"),
    [
        # trailing-slash / path redirect on the same origin
        (
            "https://peer.example/fed/inbox/abc",
            "/fed/inbox/abc/",
            "https://peer.example/fed/inbox/abc/",
        ),
        (
            "https://peer.example/fed/inbox/abc",
            "abc/",
            "https://peer.example/fed/inbox/abc/",
        ),
        # http → https upgrade on the same host
        (
            "http://peer.example/fed/inbox/abc",
            "https://peer.example/fed/inbox/abc",
            "https://peer.example/fed/inbox/abc",
        ),
        (
            "http://peer.example:8123/i",
            "https://PEER.example:8123/i",
            "https://PEER.example:8123/i",
        ),
    ],
)
def test_safe_redirect_target_allows_same_host_hops(origin, location, expected):
    assert safe_redirect_target(origin, location) == expected


@pytest.mark.parametrize(
    ("origin", "location"),
    [
        # another host — the whole point
        ("https://peer.example/i", "https://evil.example/i"),
        ("https://peer.example/i", "//evil.example/i"),
        ("https://peer.example/i", "https://peer.example.evil.example/i"),
        # downgrade
        ("https://peer.example/i", "http://peer.example/i"),
        # same scheme, another port = another service
        ("http://peer.example:8123/i", "http://peer.example:9000/i"),
        # upgrade onto an arbitrary port
        ("http://peer.example:8123/i", "https://peer.example:9443/i"),
        # not a household address at all
        ("https://peer.example/i", "file:///etc/passwd"),
        ("https://peer.example/i", "https://user:pw@peer.example/i"),
        # nothing / itself
        ("https://peer.example/i", ""),
        ("https://peer.example/i", None),
        ("https://peer.example/i", "https://peer.example/i"),
    ],
)
def test_safe_redirect_target_refuses(origin, location):
    assert safe_redirect_target(origin, location) is None


# ─── post_to_peer over a real server ─────────────────────────────────────


@pytest.fixture
async def server():
    hits: dict[str, int] = {}

    def _count(name: str) -> None:
        hits[name] = hits.get(name, 0) + 1

    async def inbox(request: web.Request) -> web.Response:
        _count("inbox")
        return web.json_response({"body": await request.json()})

    async def to_other_host(request: web.Request) -> web.Response:
        _count("to_other_host")
        # ``localhost`` is the same machine but NOT the host the URL named
        # (``127.0.0.1``) — aiohttp's default would follow it.
        port = request.url.port
        raise web.HTTPPermanentRedirect(f"http://localhost:{port}/inbox")

    async def slash(request: web.Request) -> web.Response:
        _count("slash")
        raise web.HTTPPermanentRedirect("/inbox")

    async def loop_a(request: web.Request) -> web.Response:
        _count("loop_a")
        raise web.HTTPTemporaryRedirect("/loop-b")

    async def loop_b(request: web.Request) -> web.Response:
        _count("loop_b")
        raise web.HTTPTemporaryRedirect("/loop-a")

    async def see_other(request: web.Request) -> web.Response:
        _count("see_other")
        raise web.HTTPSeeOther("/inbox")

    app = web.Application()
    app.router.add_post("/inbox", inbox)
    app.router.add_post("/to-other-host", to_other_host)
    app.router.add_post("/slash", slash)
    app.router.add_post("/loop-a", loop_a)
    app.router.add_post("/loop-b", loop_b)
    app.router.add_post("/see-other", see_other)
    srv = TestServer(app, host="127.0.0.1")
    await srv.start_server()
    async with aiohttp.ClientSession() as session:
        yield srv, session, hits
    await srv.close()


def _url(srv: TestServer, path: str) -> str:
    return f"http://127.0.0.1:{srv.port}{path}"


async def test_plain_post_is_unchanged(server):
    srv, session, hits = server
    async with post_to_peer(session, _url(srv, "/inbox"), json={"x": 1}) as resp:
        assert resp.status == 200
        assert (await resp.json()) == {"body": {"x": 1}}
    assert hits == {"inbox": 1}


async def test_redirect_to_another_host_is_refused(server, caplog):
    srv, session, hits = server
    with caplog.at_level(logging.WARNING, logger="socialhome.peer_http"):
        async with post_to_peer(
            session, _url(srv, "/to-other-host?token=s3cret"), json={"x": 1}
        ) as resp:
            assert resp.status == 308
    # The envelope never reached the redirect target.
    assert hits == {"to_other_host": 1}
    text = caplog.text
    assert "127.0.0.1" in text and "localhost" in text
    # Host only — never the path or query (inbox ids, tokens).
    assert "s3cret" not in text and "/to-other-host" not in text
    assert "/inbox" not in text


async def test_same_host_redirect_is_followed_once_with_the_body(server):
    srv, session, hits = server
    async with post_to_peer(session, _url(srv, "/slash"), json={"x": 2}) as resp:
        assert resp.status == 200
        assert (await resp.json()) == {"body": {"x": 2}}
    assert hits == {"slash": 1, "inbox": 1}


async def test_redirect_loop_is_refused_after_one_hop(server):
    srv, session, hits = server
    async with post_to_peer(session, _url(srv, "/loop-a"), json={}) as resp:
        assert resp.status == 307
    assert hits == {"loop_a": 1, "loop_b": 1}


async def test_see_other_is_not_followed(server):
    """303 turns a POST into a GET — an envelope is never re-sent as one."""
    srv, session, hits = server
    async with post_to_peer(session, _url(srv, "/see-other"), json={}) as resp:
        assert resp.status == 303
    assert hits == {"see_other": 1}


# ─── post_to_peer: the https-upgrade hop (no TLS server needed) ──────────


class _Resp:
    def __init__(self, status: int, location: str | None = None) -> None:
        self.status = status
        self.headers = {"Location": location} if location else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ScriptedClient:
    def __init__(self, script: dict[str, _Resp]) -> None:
        self._script = script
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, **kw):
        self.calls.append((url, kw))
        return self._script[url]


async def test_http_to_https_upgrade_is_followed_once():
    client = _ScriptedClient(
        {
            "http://peer.example/i/abc": _Resp(301, "https://peer.example/i/abc"),
            "https://peer.example/i/abc": _Resp(202),
        }
    )
    async with post_to_peer(client, "http://peer.example/i/abc", data=b"{}") as resp:
        assert resp.status == 202
    assert [u for u, _ in client.calls] == [
        "http://peer.example/i/abc",
        "https://peer.example/i/abc",
    ]
    # Automatic following is off on BOTH hops, and the body is re-sent.
    assert all(kw["allow_redirects"] is False for _, kw in client.calls)
    assert all(kw["data"] == b"{}" for _, kw in client.calls)


async def test_https_to_http_downgrade_is_refused():
    client = _ScriptedClient(
        {"https://peer.example/i": _Resp(308, "http://peer.example/i")}
    )
    async with post_to_peer(client, "https://peer.example/i", json={}) as resp:
        assert resp.status == 308
    assert len(client.calls) == 1


async def test_caller_cannot_turn_automatic_following_back_on():
    client = _ScriptedClient({"https://peer.example/i": _Resp(200)})
    async with post_to_peer(
        client, "https://peer.example/i", json={}, allow_redirects=True
    ) as resp:
        assert resp.status == 200
    assert client.calls[0][1]["allow_redirects"] is False


# ─── Guard: no outbound call to a peer / connection server follows 3xx ───

#: Modules whose HTTP calls target a URL that came from outside (a household
#: inbox, a connection server, a cluster peer). Each ``async with x.post(`` /
#: ``x.get(`` in them must pass ``allow_redirects=False`` — or go through
#: :func:`post_to_peer`, which forces it.
_PEER_HTTP_MODULES = (
    "socialhome/app.py",
    "socialhome/federation/federation_service.py",
    "socialhome/federation/transport.py",
    "socialhome/federation/peer_pairing_client.py",
    "socialhome/global_server/federation.py",
    "socialhome/global_server/cluster.py",
    "socialhome/services/gfs_connection_service.py",
    "socialhome/services/gfs_envelope_sender.py",
    "socialhome/services/gfs_space_mirror_service.py",
    "socialhome/services/space_subscriber_key_outbound.py",
    "socialhome/services/public_space_discovery_service.py",
    "socialhome/services/highlight_publication_service.py",
    "socialhome/services/highlight_signaling_handler.py",
    "socialhome/services/moment_public_service.py",
    "socialhome/services/moment_public_outbound.py",
    "socialhome/services/moment_public_signaling_handler.py",
)

#: ``app._download_bytes`` fetches an app bundle from the operator-configured
#: catalog, not from a peer — the one reviewed exception in these modules.
_ALLOWED_FOLLOWING = {("socialhome/app.py", "_download_bytes")}


def _following_calls(path: str) -> list[str]:
    tree = ast.parse((_REPO_ROOT / path).read_text())
    offenders: list[str] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if (path, func.name) in _ALLOWED_FOLLOWING:
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.AsyncWith):
                continue
            for item in node.items:
                call = item.context_expr
                if not (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr in {"post", "get"}
                ):
                    continue
                value = {k.arg: k.value for k in call.keywords}.get("allow_redirects")
                if not (isinstance(value, ast.Constant) and value.value is False):
                    offenders.append(f"{path}:{call.lineno} ({func.name})")
    return offenders


def test_no_peer_http_call_follows_redirects():
    offenders = [o for p in _PEER_HTTP_MODULES for o in _following_calls(p)]
    assert offenders == [], offenders
