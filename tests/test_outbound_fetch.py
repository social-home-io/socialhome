"""Tests for :mod:`socialhome.outbound_fetch` — the SSRF guard every
user-supplied URL (link previews) goes through.

Refusal classes run without any network: the guard refuses before a socket
opens. The behaviour tests (redirect re-vetting, rebinding, size caps,
slow-loris) run against a local aiohttp server; since the guard refuses
loopback by design, those tests patch the address policy to treat
``127.0.0.1`` as "public" and everything else as private — never an
env-var switch in production code.
"""

from __future__ import annotations

import asyncio
import ipaddress

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from socialhome import outbound_fetch
from socialhome.outbound_fetch import (
    OutboundFetcher,
    OutboundFetchRefused,
    _PinnedResolver,
    check_url_shape,
    is_public_address,
    parse_ip_literal,
)

HTML = frozenset({"text/html"})

pytestmark = pytest.mark.security


def _resolver(mapping: dict[str, list[str]]):
    calls: list[str] = []

    async def resolve(host: str, port: int) -> list[str]:
        calls.append(host)
        if host not in mapping:
            raise OSError("NXDOMAIN")
        return mapping[host]

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


# ─── address policy ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",
        "127.255.255.254",
        "10.0.0.1",
        "172.16.5.4",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # CGNAT
        "0.0.0.0",
        "224.0.0.1",
        "239.255.255.250",
        "255.255.255.255",
        "192.0.2.1",  # documentation
        "198.18.0.1",  # benchmarking
        "240.0.0.1",  # reserved
        "::1",
        "::",
        "fe80::1",
        "fd00::1",  # ULA
        "fc00::1",
        "fec0::1",  # site-local
        "ff02::1",
        "ff0e::1",  # global-scope multicast
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "::ffff:169.254.169.254",
        "2002:7f00:1::1",  # 6to4 of 127.0.0.1
        "2002:c0a8:101::1",  # 6to4 of 192.168.1.1
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo → 192.0.2.45
        "64:ff9b:1::a00:1",  # local-use NAT64
        "2001:db8::1",  # documentation
    ],
)
def test_non_public_addresses_refused(addr: str) -> None:
    assert is_public_address(ipaddress.ip_address(addr)) is False


@pytest.mark.parametrize(
    "addr", ["8.8.8.8", "93.184.216.34", "1.1.1.1", "2606:4700:4700::1111"]
)
def test_public_addresses_allowed(addr: str) -> None:
    assert is_public_address(ipaddress.ip_address(addr)) is True


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("2130706433", "127.0.0.1"),
        ("0x7f000001", "127.0.0.1"),
        ("0177.0.0.1", "127.0.0.1"),
        ("0x7f.0.0.1", "127.0.0.1"),
        ("127.1", "127.0.0.1"),
        ("017700000001", "127.0.0.1"),
        ("3232235777", "192.168.1.1"),
        ("[::1]", "::1"),
        ("::ffff:7f00:1", "::ffff:7f00:1"),
        ("8.8.8.8", "8.8.8.8"),
    ],
)
def test_parse_ip_literal_legacy_forms(host: str, expected: str) -> None:
    assert parse_ip_literal(host) == ipaddress.ip_address(expected)


@pytest.mark.parametrize("host", ["example.com", "deadbeef.example", "abc", ""])
def test_parse_ip_literal_host_names(host: str) -> None:
    assert parse_ip_literal(host) is None


# ─── URL shape ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("file:///etc/passwd", "scheme"),
        ("ftp://example.com/", "scheme"),
        ("data:text/html,<b>x</b>", "scheme"),
        ("javascript:alert(1)", "scheme"),
        ("gopher://example.com/", "scheme"),
        ("ws://example.com/", "scheme"),
        ("//example.com/", "scheme"),
        ("https://user:pw@example.com/", "userinfo"),
        ("https://user@example.com/", "userinfo"),
        ("http://example.com:8123/", "port"),
        ("http://example.com:22/", "port"),
        ("https://example.com:8443/", "port"),
        ("http:///nohost", "malformed"),
        ("http://example.com:99999/", "malformed"),
        ("http://exa mple.com/", "malformed"),
        ("http://example.com/\r\nX: y", "malformed"),
        ("", "malformed"),
        ("https://example.com/" + "a" * 3000, "malformed"),
    ],
)
def test_url_shape_refusals(url: str, reason: str) -> None:
    with pytest.raises(OutboundFetchRefused) as info:
        check_url_shape(url)
    assert info.value.reason == reason


@pytest.mark.parametrize(
    ("url", "port"),
    [
        ("http://example.com/", 80),
        ("https://example.com/", 443),
        ("HTTPS://Example.COM:443/x?y=1#z", 443),
        ("http://example.com:80/", 80),
    ],
)
def test_url_shape_accepts(url: str, port: int) -> None:
    assert check_url_shape(url)[2] == port


# ─── refusal before any socket opens ─────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://0177.0.0.1/",
        "http://127.1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[fd12:3456::1]/",
        "http://[fe80::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.1.1/",
        "http://0.0.0.0/",
        "http://192.168.1.1/",
    ],
)
async def test_literal_private_hosts_refused(url: str) -> None:
    resolve = _resolver({})
    fetcher = OutboundFetcher(resolver=resolve)
    with pytest.raises(OutboundFetchRefused) as info:
        await fetcher.fetch(url, accept=HTML, max_bytes=1024)
    assert info.value.reason == "private_address"
    assert resolve.calls == []  # literal — never handed to DNS


@pytest.mark.parametrize(
    "answers",
    [
        ["127.0.0.1"],
        ["10.1.2.3"],
        ["::1"],
        ["fd00::5"],
        ["93.184.216.34", "192.168.0.10"],  # one private answer is enough
        ["::ffff:10.0.0.1"],
        ["fe80::1%eth0"],
    ],
)
async def test_host_resolving_to_private_refused(answers: list[str]) -> None:
    fetcher = OutboundFetcher(resolver=_resolver({"evil.example": answers}))
    with pytest.raises(OutboundFetchRefused) as info:
        await fetcher.fetch("https://evil.example/", accept=HTML, max_bytes=1024)
    assert info.value.reason == "private_address"


async def test_dns_failure_refused() -> None:
    fetcher = OutboundFetcher(resolver=_resolver({}))
    with pytest.raises(OutboundFetchRefused) as info:
        await fetcher.fetch("https://nx.example/", accept=HTML, max_bytes=1024)
    assert info.value.reason == "dns"


async def test_empty_or_garbage_dns_answer_refused() -> None:
    fetcher = OutboundFetcher(
        resolver=_resolver({"a.example": [], "b.example": ["not-an-ip"]})
    )
    for host in ("a.example", "b.example"):
        with pytest.raises(OutboundFetchRefused) as info:
            await fetcher.fetch(f"https://{host}/", accept=HTML, max_bytes=1024)
        assert info.value.reason == "dns"


async def test_slow_dns_hits_total_timeout() -> None:
    async def slow(host: str, port: int) -> list[str]:
        await asyncio.sleep(5)
        return ["8.8.8.8"]

    fetcher = OutboundFetcher(resolver=slow, total_timeout_s=0.2)
    with pytest.raises(OutboundFetchRefused) as info:
        await fetcher.fetch("https://slow.example/", accept=HTML, max_bytes=1024)
    assert info.value.reason == "timeout"


async def test_system_resolve_returns_addresses() -> None:
    addrs = await outbound_fetch.system_resolve("localhost", 80)
    assert addrs
    assert all(ipaddress.ip_address(a.split("%")[0]) for a in addrs)


# ─── pinned resolver ─────────────────────────────────────────────────────


async def test_pinned_resolver_answers_only_its_host() -> None:
    r = _PinnedResolver("Example.COM", ["93.184.216.34", "2606:2800::1"])
    out = await r.resolve("example.com", 443)
    assert [x["host"] for x in out] == ["93.184.216.34", "2606:2800::1"]
    assert out[0]["hostname"] == "example.com"
    assert out[0]["port"] == 443
    with pytest.raises(OSError):
        await r.resolve("other.example", 443)
    await r.close()


# ─── behaviour against a local server ────────────────────────────────────


@pytest.fixture
def loopback_is_public(monkeypatch):
    """Treat 127.0.0.1 as the only public address, keep everything else
    private — so the real server can answer while every other rule holds."""

    def policy(ip) -> bool:
        return str(ip) == "127.0.0.1"

    monkeypatch.setattr(outbound_fetch, "is_public_address", policy)
    return monkeypatch


async def _serve(routes: list[web.RouteDef]) -> TestServer:
    app = web.Application()
    app.add_routes(routes)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    return server


def _fetcher_for(server: TestServer, monkeypatch, mapping=None, **kw):
    monkeypatch.setattr(outbound_fetch, "ALLOWED_PORTS", frozenset({server.port}))
    resolve = _resolver(mapping or {"site.test": ["127.0.0.1"]})
    return OutboundFetcher(resolver=resolve, **kw), resolve


async def test_fetch_ok_is_anonymous(loopback_is_public) -> None:
    seen: dict = {}

    async def page(request: web.Request) -> web.Response:
        seen.update(request.headers)
        return web.Response(
            text="<html><title>x</title></html>",
            content_type="text/html",
            charset="utf-8",
        )

    server = await _serve([web.get("/", page)])
    try:
        fetcher, _ = _fetcher_for(server, loopback_is_public)
        res = await fetcher.fetch(
            f"http://site.test:{server.port}/", accept=HTML, max_bytes=4096
        )
    finally:
        await server.close()
    assert res.body.startswith(b"<html>")
    assert res.content_type == "text/html"
    assert res.charset == "utf-8"
    assert res.truncated is False
    assert "Cookie" not in seen
    assert "Authorization" not in seen
    assert seen["User-Agent"] == outbound_fetch.USER_AGENT
    assert "site.test" in seen["Host"]


async def test_rebinding_second_answer_never_asked(loopback_is_public) -> None:
    """The check and the connect use ONE resolution: a DNS server that
    answers public-then-private never gets the second question."""

    async def page(request: web.Request) -> web.Response:
        return web.Response(text="ok", content_type="text/html")

    server = await _serve([web.get("/", page)])
    answers = iter([["127.0.0.1"], ["10.0.0.1"], ["10.0.0.1"]])
    calls = 0

    async def rebinding(host: str, port: int) -> list[str]:
        nonlocal calls
        calls += 1
        return next(answers)

    try:
        loopback_is_public.setattr(
            outbound_fetch, "ALLOWED_PORTS", frozenset({server.port})
        )
        fetcher = OutboundFetcher(resolver=rebinding)
        res = await fetcher.fetch(
            f"http://rebind.test:{server.port}/", accept=HTML, max_bytes=4096
        )
    finally:
        await server.close()
    assert res.body == b"ok"
    assert calls == 1


async def test_redirect_to_private_literal_refused(loopback_is_public) -> None:
    async def hop(request: web.Request) -> web.Response:
        raise web.HTTPFound("http://10.0.0.1/admin")

    server = await _serve([web.get("/", hop)])
    try:
        fetcher, _ = _fetcher_for(server, loopback_is_public)
        loopback_is_public.setattr(
            outbound_fetch, "ALLOWED_PORTS", frozenset({server.port, 80})
        )
        with pytest.raises(OutboundFetchRefused) as info:
            await fetcher.fetch(
                f"http://site.test:{server.port}/", accept=HTML, max_bytes=4096
            )
    finally:
        await server.close()
    assert info.value.reason == "private_address"


async def test_redirect_to_host_resolving_private_refused(loopback_is_public) -> None:
    async def hop(request: web.Request) -> web.Response:
        raise web.HTTPMovedPermanently(f"http://internal.test:{request.url.port}/")

    server = await _serve([web.get("/", hop)])
    try:
        fetcher, resolve = _fetcher_for(
            server,
            loopback_is_public,
            mapping={"site.test": ["127.0.0.1"], "internal.test": ["192.168.1.2"]},
        )
        with pytest.raises(OutboundFetchRefused) as info:
            await fetcher.fetch(
                f"http://site.test:{server.port}/", accept=HTML, max_bytes=4096
            )
    finally:
        await server.close()
    assert info.value.reason == "private_address"
    assert resolve.calls == ["site.test", "internal.test"]


@pytest.mark.parametrize(
    ("location", "reason"),
    [
        ("file:///etc/passwd", "scheme"),
        ("gopher://site.test/", "scheme"),
        ("http://site.test:22/", "port"),
        ("http://u:p@site.test/", "userinfo"),
    ],
)
async def test_redirect_hop_shape_refused(
    loopback_is_public, location: str, reason: str
) -> None:
    async def hop(request: web.Request) -> web.Response:
        return web.Response(status=307, headers={"Location": location})

    server = await _serve([web.get("/", hop)])
    try:
        fetcher, _ = _fetcher_for(server, loopback_is_public)
        with pytest.raises(OutboundFetchRefused) as info:
            await fetcher.fetch(
                f"http://site.test:{server.port}/", accept=HTML, max_bytes=4096
            )
    finally:
        await server.close()
    assert info.value.reason == reason


async def test_redirects_followed_up_to_limit_then_refused(loopback_is_public) -> None:
    async def hop(request: web.Request) -> web.Response:
        n = int(request.match_info["n"])
        if n >= 10:
            return web.Response(text="end", content_type="text/html")
        return web.Response(
            status=302,
            headers={"Location": f"/r/{n + 1}", "Set-Cookie": "sid=secret"},
        )

    cookies: list[str | None] = []

    async def spy(request: web.Request) -> web.Response:
        cookies.append(request.headers.get("Cookie"))
        return await hop(request)

    server = await _serve([web.get("/r/{n}", spy)])
    try:
        fetcher, _ = _fetcher_for(server, loopback_is_public)
        base = f"http://site.test:{server.port}"
        # 3 redirects allowed: /r/7 → 8 → 9 → 10 answers.
        res = await fetcher.fetch(f"{base}/r/7", accept=HTML, max_bytes=4096)
        assert res.body == b"end"
        assert res.url.endswith("/r/10")
        with pytest.raises(OutboundFetchRefused) as info:
            await fetcher.fetch(f"{base}/r/6", accept=HTML, max_bytes=4096)
    finally:
        await server.close()
    assert info.value.reason == "too_many_redirects"
    assert all(c is None for c in cookies)  # no cookie jar


async def test_redirect_without_location_refused(loopback_is_public) -> None:
    async def hop(request: web.Request) -> web.Response:
        return web.Response(status=302)

    server = await _serve([web.get("/", hop)])
    try:
        fetcher, _ = _fetcher_for(server, loopback_is_public)
        with pytest.raises(OutboundFetchRefused) as info:
            await fetcher.fetch(
                f"http://site.test:{server.port}/", accept=HTML, max_bytes=4096
            )
    finally:
        await server.close()
    assert info.value.reason == "bad_redirect"


async def test_non_200_and_wrong_type_refused(loopback_is_public) -> None:
    async def missing(request: web.Request) -> web.Response:
        return web.Response(status=404, text="nope")

    async def js(request: web.Request) -> web.Response:
        return web.Response(text="x", content_type="application/javascript")

    server = await _serve([web.get("/404", missing), web.get("/js", js)])
    try:
        fetcher, _ = _fetcher_for(server, loopback_is_public)
        base = f"http://site.test:{server.port}"
        with pytest.raises(OutboundFetchRefused) as a:
            await fetcher.fetch(f"{base}/404", accept=HTML, max_bytes=4096)
        with pytest.raises(OutboundFetchRefused) as b:
            await fetcher.fetch(f"{base}/js", accept=HTML, max_bytes=4096)
    finally:
        await server.close()
    assert a.value.reason == "status"
    assert b.value.reason == "content_type"


async def test_oversized_bodies(loopback_is_public) -> None:
    big = b"a" * (100 * 1024)

    async def declared(request: web.Request) -> web.Response:
        return web.Response(body=big, content_type="text/html")

    async def chunked(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "text/html"})
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        for _ in range(10):
            await resp.write(b"b" * 10240)
        await resp.write_eof()
        return resp

    server = await _serve([web.get("/d", declared), web.get("/c", chunked)])
    try:
        fetcher, _ = _fetcher_for(server, loopback_is_public)
        base = f"http://site.test:{server.port}"
        for path in ("/d", "/c"):
            with pytest.raises(OutboundFetchRefused) as info:
                await fetcher.fetch(f"{base}{path}", accept=HTML, max_bytes=50_000)
            assert info.value.reason == "too_large"
        # HTML mode: stop reading at the cap and keep the head.
        res = await fetcher.fetch(
            f"{base}/c", accept=HTML, max_bytes=50_000, truncate=True
        )
        assert len(res.body) == 50_000
        assert res.truncated is True
        res = await fetcher.fetch(
            f"{base}/d", accept=HTML, max_bytes=50_000, truncate=True
        )
        assert len(res.body) == 50_000
    finally:
        await server.close()


async def test_slow_loris_body_cut_off(loopback_is_public) -> None:
    async def drip(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "text/html"})
        await resp.prepare(request)
        for _ in range(100):
            await resp.write(b"<")
            await asyncio.sleep(0.1)
        return resp

    server = await _serve([web.get("/", drip)])
    try:
        fetcher, _ = _fetcher_for(server, loopback_is_public, total_timeout_s=0.6)
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(OutboundFetchRefused) as info:
            await fetcher.fetch(
                f"http://site.test:{server.port}/", accept=HTML, max_bytes=4096
            )
        elapsed = loop.time() - started
    finally:
        await server.close()
    assert info.value.reason == "timeout"
    assert elapsed < 2.0


async def test_stalled_read_hits_read_timeout(loopback_is_public) -> None:
    async def stall(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "text/html"})
        await resp.prepare(request)
        await resp.write(b"<html>")
        await asyncio.sleep(3)
        return resp

    loopback_is_public.setattr(outbound_fetch, "READ_TIMEOUT_S", 0.2)
    server = await _serve([web.get("/", stall)])
    try:
        fetcher, _ = _fetcher_for(server, loopback_is_public)
        with pytest.raises(OutboundFetchRefused) as info:
            await fetcher.fetch(
                f"http://site.test:{server.port}/", accept=HTML, max_bytes=4096
            )
    finally:
        await server.close()
    assert info.value.reason in {"network", "timeout"}


async def test_connection_refused_is_network(loopback_is_public) -> None:
    server = await _serve([])
    port = server.port
    await server.close()
    loopback_is_public.setattr(outbound_fetch, "ALLOWED_PORTS", frozenset({port}))
    fetcher = OutboundFetcher(resolver=_resolver({"site.test": ["127.0.0.1"]}))
    with pytest.raises(OutboundFetchRefused) as info:
        await fetcher.fetch(f"http://site.test:{port}/", accept=HTML, max_bytes=4096)
    assert info.value.reason == "network"
