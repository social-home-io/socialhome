"""Tests for the dashboard aiohttp app: session guard, CSRF, headers."""

from __future__ import annotations

import re
from pathlib import Path

import aiohttp
import pytest
from yarl import URL

import socialhome.i18n as i18n_pkg
from socialhome.dashboard.app import (
    DASHBOARD_CSP_DIRECTIVES,
    build_dashboard_app,
    build_dashboard_csp,
    host_allowed,
)
from socialhome.dashboard.auth import COOKIE_NAME, RENEW_AFTER_S, DashboardAuth
from socialhome.dashboard.render import DashboardRenderer
from socialhome.i18n import Catalog

from .conftest import PASSWORD, default_theme
from .factories import FakeDashboardService

RENDERER = DashboardRenderer(
    Catalog.from_directory(Path(i18n_pkg.__file__).parent / "messages")
)


async def _name() -> str:
    return "Home"


async def _tz() -> str:
    return "UTC"


@pytest.fixture
def auth(settings_service, clock) -> DashboardAuth:
    return DashboardAuth(settings_service, clock=clock)


@pytest.fixture
async def client(aiohttp_client, settings_service, auth):
    await settings_service.set_password(actor_is_admin=True, password=PASSWORD)
    app = build_dashboard_app(
        settings_service=settings_service,
        auth=auth,
        renderer=RENDERER,
        household_name=_name,
        household_tz=_tz,
        household_theme=default_theme,
        snapshots=FakeDashboardService(),
    )
    return await aiohttp_client(app, cookie_jar=aiohttp.DummyCookieJar())


def _origin(client) -> str:
    return f"http://{client.host}:{client.port}"


def _browser(client, page: str = "/login") -> dict[str, str]:
    """What a browser sends on a form POST from our own page, given our
    ``Referrer-Policy: same-origin``: the real origin + the full referer."""
    return {"Origin": _origin(client), "Referer": f"{_origin(client)}{page}"}


async def _cookie(auth: DashboardAuth, keep: bool = True) -> dict[str, str]:
    return {"Cookie": f"{COOKIE_NAME}={await auth.issue(keep)}"}


# ─── Session guard ───────────────────────────────────────────────────────


async def test_unauthenticated_get_redirects_to_login(client):
    resp = await client.get("/", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/login?next=%2F"


async def test_unauthenticated_unknown_path_redirects(client):
    resp = await client.get("/people/x?a=1", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/login?next=%2Fpeople%2Fx%3Fa%3D1"


async def test_unauthenticated_fragment_is_401(client):
    resp = await client.get("/fragment/x", allow_redirects=False)
    assert resp.status == 401


async def test_unauthenticated_post_is_401(client):
    resp = await client.post(
        "/anything", headers=_browser(client), allow_redirects=False
    )
    assert resp.status == 401


async def test_unauthenticated_head_is_401(client):
    """HEAD isn't a page view: no login redirect (and views answer only GET)."""
    resp = await client.head("/", allow_redirects=False)
    assert resp.status == 401


async def test_ingress_headers_are_not_honoured(client):
    resp = await client.get(
        "/",
        headers={"X-Hass-Source": "core.ingress", "X-Remote-User-Name": "admin"},
        allow_redirects=False,
    )
    assert resp.status == 302
    assert resp.headers["Location"].startswith("/login")


async def test_authenticated_get_passes_and_touches(client, auth, clock):
    resp = await client.get("/", headers=await _cookie(auth), allow_redirects=False)
    assert resp.status == 200
    assert auth.last_seen_at == clock.now
    assert "Set-Cookie" not in resp.headers


async def test_epoch_bump_signs_tablet_out(client, auth, settings_service):
    headers = await _cookie(auth)
    await settings_service.revoke_sessions(actor_is_admin=True)
    resp = await client.get("/", headers=headers, allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/login?next=%2F"


async def test_old_cookie_is_renewed(client, auth, clock):
    headers = await _cookie(auth, keep=True)
    clock.advance(RENEW_AFTER_S + 1)
    resp = await client.get("/", headers=headers, allow_redirects=False)
    assert resp.status == 200
    morsel = resp.cookies[COOKIE_NAME]
    claims = await auth.verify(morsel.value)
    assert claims is not None
    assert claims.issued_at == int(clock.now)
    assert claims.keep is True
    assert morsel["max-age"] == str(365 * 86400)


async def test_renewed_cookie_secure_behind_https_address(
    client, auth, clock, settings_service
):
    await settings_service.update(
        actor_is_admin=True, patch={"address": "https://dash.example.lan"}
    )
    headers = await _cookie(auth, keep=True)
    clock.advance(RENEW_AFTER_S + 1)
    resp = await client.get(
        "/", headers={**headers, "Host": "dash.example.lan"}, allow_redirects=False
    )
    assert resp.status == 200
    assert resp.cookies[COOKIE_NAME]["secure"] is True


async def test_not_found_person_page_never_renews(client, auth, clock):
    """Only a page that actually rendered renews: a 404 for an unknown or
    hidden person sets no cookie."""
    headers = await _cookie(auth, keep=True)
    clock.advance(RENEW_AFTER_S + 1)
    resp = await client.get("/p/nobody", headers=headers, allow_redirects=False)
    assert resp.status == 404
    assert COOKIE_NAME not in resp.cookies


async def test_fresh_cookie_not_renewed(client, auth, clock):
    headers = await _cookie(auth, keep=True)
    clock.advance(RENEW_AFTER_S)
    resp = await client.get("/", headers=headers, allow_redirects=False)
    assert COOKIE_NAME not in resp.cookies


async def test_public_paths(client):
    resp = await client.get("/healthz")
    assert resp.status == 200
    assert await resp.text() == "ok"
    resp = await client.get("/static/dashboard.css")
    assert resp.status == 200
    assert "text/css" in resp.headers["Content-Type"]
    resp = await client.get("/login", allow_redirects=False)
    assert resp.status == 200


@pytest.mark.parametrize(
    "path", ["/static/%2e%2e/app.py", "/static/..%2fapp.py", "/static/%2e%2e%2fauth.py"]
)
async def test_static_traversal_refused(client, path):
    resp = await client.get(URL(path, encoded=True), allow_redirects=False)
    assert resp.status in (403, 404)
    assert "build_dashboard_app" not in await resp.text()


# ─── CSRF ────────────────────────────────────────────────────────────────


async def test_post_with_foreign_origin_is_403(client):
    resp = await client.post(
        "/login",
        data={"password": PASSWORD},
        headers={"Origin": "https://evil.example"},
        allow_redirects=False,
    )
    assert resp.status == 403
    assert "Set-Cookie" not in resp.headers


async def test_post_with_opaque_null_origin_is_403(client):
    """``Origin: null`` is what a browser sends for a cross-site POST from a
    ``no-referrer`` / sandboxed page — never for our own forms, whose
    ``Referrer-Policy: same-origin`` keeps the real origin."""
    resp = await client.post(
        "/login",
        data={"password": PASSWORD},
        headers={"Origin": "null"},
        allow_redirects=False,
    )
    assert resp.status == 403


async def test_post_without_origin_or_referer_is_403(client):
    resp = await client.post(
        "/login", data={"password": PASSWORD}, allow_redirects=False
    )
    assert resp.status == 403


async def test_browser_form_post_passes(client):
    resp = await client.post(
        "/login",
        data={"password": PASSWORD},
        headers=_browser(client),
        allow_redirects=False,
    )
    assert resp.status == 303


async def test_post_with_same_origin_referer_only_passes(client):
    resp = await client.post(
        "/login",
        data={"password": PASSWORD},
        headers={"Referer": f"{_origin(client)}/login"},
        allow_redirects=False,
    )
    assert resp.status == 303


async def test_post_with_foreign_referer_is_403(client):
    resp = await client.post(
        "/login",
        data={"password": PASSWORD},
        headers={"Referer": "https://evil.example/login"},
        allow_redirects=False,
    )
    assert resp.status == 403


async def test_post_with_malformed_origin_is_403(client):
    resp = await client.post(
        "/login",
        data={"password": PASSWORD},
        headers={"Origin": "http://[::1"},
        allow_redirects=False,
    )
    assert resp.status == 403


# ─── Headers ─────────────────────────────────────────────────────────────


def test_csp_directives_exact():
    assert build_dashboard_csp() == (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    )
    flat = " ".join(src for srcs in DASHBOARD_CSP_DIRECTIVES.values() for src in srcs)
    assert "unsafe-inline" not in flat
    assert "unsafe-eval" not in flat
    assert "http" not in flat


def test_csp_admits_inline_style_hashes():
    csp = build_dashboard_csp(["a{}", "a{}"])
    assert csp.count("'sha256-") == 1
    assert "style-src 'self' 'sha256-" in csp


async def test_security_headers_on_html(client, auth):
    for resp in (
        await client.get("/login"),
        await client.get("/", headers=await _cookie(auth)),
    ):
        # A board page admits its one inline <style> by hash; nothing else.
        styles = re.findall(r"<style>(.*?)</style>", await resp.text(), re.S)
        assert resp.headers["Content-Security-Policy"] == build_dashboard_csp(styles)
        assert resp.headers["Cache-Control"] == "no-store"
        assert resp.headers["Referrer-Policy"] == "same-origin"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["Permissions-Policy"] == (
            "camera=(), microphone=(), geolocation=(), screen-wake-lock=(self)"
        )
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Content-Type"].startswith("text/html")


async def test_redirects_carry_headers(client):
    resp = await client.get("/", allow_redirects=False)
    assert resp.headers["Content-Security-Policy"] == build_dashboard_csp()
    assert resp.headers["Referrer-Policy"] == "same-origin"


async def test_static_is_revalidated_by_etag(client):
    resp = await client.get("/static/dashboard.css")
    assert resp.headers["Cache-Control"] == "no-cache"
    etag = resp.headers["ETag"]
    assert etag == RENDERER.static_asset("dashboard.css").etag
    again = await client.get("/static/dashboard.css", headers={"If-None-Match": etag})
    assert again.status == 304
    assert await again.read() == b""
    stale = await client.get(
        "/static/dashboard.css", headers={"If-None-Match": '"other"'}
    )
    assert stale.status == 200


async def test_static_unknown_is_404(client):
    assert (await client.get("/static/nope.css")).status == 404
    assert (await client.get("/static/")).status == 404


async def test_oversized_body_is_413(client):
    resp = await client.post(
        "/login",
        data={"password": "x" * 40_000},
        headers=_browser(client),
        allow_redirects=False,
    )
    assert resp.status == 413


async def test_redirects_are_not_cacheable(client):
    resp = await client.get("/", allow_redirects=False)
    assert resp.headers["Cache-Control"] == "no-store"


# ─── Host allow-list (DNS rebinding) ─────────────────────────────────────


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "192.168.1.20:8100",
        "[::1]:8100",
        "[fe80::1]",
        "localhost",
        "localhost:8100",
        "homeassistant",
        "homeassistant:8100",
        "HomeAssistant.Local",
        "homeassistant.local:8100",
        "tablet.lan",
        "nas.home",
        "box.internal",
        "house.home.arpa",
        "house.home.arpa.",
    ],
)
def test_host_allowed(host):
    assert host_allowed(host, frozenset()) is True


@pytest.mark.parametrize(
    "host",
    [
        "",
        "evil.com",
        "evil.com:8100",
        "local.evil.com",
        "evilhome.arpa",
        "evil.local.com",
        "[::1",
        "[nonsense]",
        "[::1]x",
        "[::1]:port",
        "host:port",
        "a:b:c",
        "bad_label.local",
        "-bad.local",
        ":8100",
        "evil.com.",
        # Arbitrary single-label names are no longer accepted …
        "intranet",
        "router:8100",
        # … which also refuses the decimal / hex single-label IPv4 forms.
        "2130706433",
        "0x7f000001",
    ],
)
def test_host_refused(host):
    assert host_allowed(host, frozenset()) is False


def test_host_local_machine_names():
    local = frozenset({"nas-box"})
    assert host_allowed("nas-box:8100", local) is True
    assert host_allowed("NAS-BOX", local) is True
    assert host_allowed("other-box", local) is False


def test_host_extra_allowed():
    extra = frozenset({"dash.example.com"})
    assert host_allowed("dash.example.com:8100", extra) is True
    assert host_allowed("DASH.example.com", extra) is True
    assert host_allowed("other.example.com", extra) is False


async def test_foreign_host_is_421(client):
    resp = await client.get(
        "/login", headers={"Host": "evil.com"}, allow_redirects=False
    )
    assert resp.status == 421
    assert resp.headers["Content-Type"].startswith("text/plain")
    resp = await client.post(
        "/login",
        data={"password": PASSWORD},
        headers={
            "Host": "evil.com",
            "Origin": "http://evil.com",
            "Referer": "http://evil.com/login",
        },
        allow_redirects=False,
    )
    assert resp.status == 421
    assert "Set-Cookie" not in resp.headers


@pytest.mark.parametrize("host", ["homeassistant.local:8100", "[::1]:8100", "10.0.0.5"])
async def test_local_hosts_are_served(client, host):
    resp = await client.get("/login", headers={"Host": host}, allow_redirects=False)
    assert resp.status == 200


async def test_local_hostnames_param(aiohttp_client, settings_service, auth):
    app = build_dashboard_app(
        settings_service=settings_service,
        auth=auth,
        renderer=RENDERER,
        household_name=_name,
        household_tz=_tz,
        household_theme=default_theme,
        snapshots=FakeDashboardService(),
        local_hostnames=frozenset({"Kitchen-Pi"}),
    )
    client = await aiohttp_client(app, cookie_jar=aiohttp.DummyCookieJar())
    ok = await client.get("/healthz", headers={"Host": "kitchen-pi:8100"})
    assert ok.status == 200
    ha = await client.get("/healthz", headers={"Host": "homeassistant:8100"})
    assert ha.status == 200
    bad = await client.get("/healthz", headers={"Host": "intranet"})
    assert bad.status == 421


async def test_static_head(client):
    resp = await client.head("/static/dashboard.css")
    assert resp.status == 200
    assert resp.headers["ETag"] == RENDERER.static_asset("dashboard.css").etag
    assert resp.headers["Content-Type"].startswith("text/css")
    assert await resp.read() == b""
    assert (await client.head("/static/nope.css")).status == 404


@pytest.mark.parametrize("kind", ["iterable", "callable"])
async def test_extra_hosts(aiohttp_client, settings_service, auth, kind):
    async def extra() -> frozenset[str]:
        return frozenset({"Dash.Example.com"})

    app = build_dashboard_app(
        settings_service=settings_service,
        auth=auth,
        renderer=RENDERER,
        household_name=_name,
        household_tz=_tz,
        household_theme=default_theme,
        snapshots=FakeDashboardService(),
        extra_hosts=extra if kind == "callable" else ["dash.example.com."],
    )
    client = await aiohttp_client(app, cookie_jar=aiohttp.DummyCookieJar())
    ok = await client.get("/healthz", headers={"Host": "dash.example.com:8100"})
    assert ok.status == 200
    bad = await client.get("/healthz", headers={"Host": "other.example.com"})
    assert bad.status == 421
