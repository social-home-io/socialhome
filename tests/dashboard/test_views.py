"""Tests for the dashboard views: login, setup link, logout, home."""

from __future__ import annotations

import asyncio
from unittest.mock import patch
from pathlib import Path

import aiohttp
import pytest

import socialhome.i18n as i18n_pkg
from socialhome.dashboard.app import build_dashboard_app
from socialhome.dashboard.auth import (
    COOKIE_NAME,
    THROTTLE_WINDOW_S,
    DashboardAuth,
)
from socialhome.dashboard.render import DashboardRenderer
from socialhome.dashboard.views import safe_next
from socialhome.services.dashboard_settings_service import DashboardSettingsService
from socialhome.i18n import Catalog

from .conftest import PASSWORD

RENDERER = DashboardRenderer(
    Catalog.from_directory(Path(i18n_pkg.__file__).parent / "messages")
)


@pytest.fixture
def auth(settings_service, clock) -> DashboardAuth:
    return DashboardAuth(settings_service, clock=clock)


@pytest.fixture
def household():
    return {"name": "Home"}


@pytest.fixture
async def bare_client(aiohttp_client, settings_service, auth, household):
    async def name() -> str:
        return household["name"]

    app = build_dashboard_app(
        settings_service=settings_service,
        auth=auth,
        renderer=RENDERER,
        household_name=name,
    )
    return await aiohttp_client(app, cookie_jar=aiohttp.DummyCookieJar())


@pytest.fixture
async def client(bare_client, settings_service):
    await settings_service.set_password(actor_is_admin=True, password=PASSWORD)
    return bare_client


def _origin(client, page: str = "/login") -> dict[str, str]:
    """A browser's form-POST headers under ``Referrer-Policy: same-origin``."""
    base = f"http://{client.host}:{client.port}"
    return {"Origin": base, "Referer": f"{base}{page}"}


async def _login(client, password=PASSWORD, *, keep=True, next_path=None):
    data = {"password": password}
    if keep:
        data["keep"] = "1"
    if next_path is not None:
        data["next"] = next_path
    return await client.post(
        "/login", data=data, headers=_origin(client), allow_redirects=False
    )


async def _cookie(auth: DashboardAuth) -> dict[str, str]:
    return {"Cookie": f"{COOKIE_NAME}={await auth.issue(True)}"}


# ─── safe_next ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "//evil.com",
        "///evil.com",
        "https://evil.com",
        "javascript:alert(1)",
        "/\\evil.com",
        "\\\\evil.com",
        "evil.com",
        "/ok\r\nSet-Cookie: x=1",
        "/ok\nx",
        "/a\tb",
        "/a\x00b",
        "/" + "a" * 3000,
        123,
    ],
)
def test_safe_next_rejects(raw):
    assert safe_next(raw) == "/"


@pytest.mark.parametrize("raw", ["/", "/ok?x=1", "/people/u-1", "/a#frag"])
def test_safe_next_keeps_local_paths(raw):
    assert safe_next(raw) == raw


# ─── GET /login ──────────────────────────────────────────────────────────


async def test_login_form(client):
    resp = await client.get("/login?next=%2Fpeople")
    assert resp.status == 200
    html = await resp.text()
    assert 'autocomplete="current-password"' in html
    assert "Keep this tablet signed in" in html
    assert 'name="keep" value="1" checked' in html
    assert 'name="next" value="/people"' in html
    assert "Ask a grown-up who manages Social Home" in html


async def test_login_form_sanitises_next(client):
    html = await (await client.get("/login?next=%2F%2Fevil.com")).text()
    assert 'name="next" value="/"' in html


async def test_login_when_signed_in_redirects(client, auth):
    resp = await client.get(
        "/login?next=%2Fpeople", headers=await _cookie(auth), allow_redirects=False
    )
    assert resp.status == 303
    assert resp.headers["Location"] == "/people"


async def test_login_locale_follows_settings(client, settings_service):
    await settings_service.update(actor_is_admin=True, patch={"locale": "de"})
    html = await (await client.get("/login")).text()
    assert '<html lang="de"' in html
    assert "Dashboard-Passwort" in html


async def test_login_get_without_password_is_not_set_up(bare_client):
    resp = await bare_client.get("/login")
    assert resp.status == 503
    html = await resp.text()
    assert "The dashboard isn&#39;t set up yet." in html
    assert "<form" not in html


# ─── POST /login ─────────────────────────────────────────────────────────


async def test_login_ok_keep(client, auth):
    resp = await _login(client, next_path="/people?x=1")
    assert resp.status == 303
    assert resp.headers["Location"] == "/people?x=1"
    morsel = resp.cookies[COOKIE_NAME]
    assert morsel["httponly"] is True
    assert morsel["samesite"] == "Strict"
    assert morsel["path"] == "/"
    assert morsel["max-age"] == str(365 * 86400)
    assert not morsel["secure"]
    claims = await auth.verify(morsel.value)
    assert claims is not None and claims.keep is True


async def test_login_ok_session_cookie(client, auth):
    resp = await _login(client, keep=False)
    assert resp.headers["Location"] == "/"
    morsel = resp.cookies[COOKIE_NAME]
    assert morsel["max-age"] == ""
    assert morsel["expires"] == ""
    claims = await auth.verify(morsel.value)
    assert claims is not None and claims.keep is False


async def test_login_open_redirect_blocked(client):
    resp = await _login(client, next_path="//evil.com")
    assert resp.status == 303
    assert resp.headers["Location"] == "/"


async def test_login_wrong_password(client):
    resp = await _login(client, "nope", keep=False, next_path="/people")
    assert resp.status == 401
    assert COOKIE_NAME not in resp.cookies
    html = await resp.text()
    assert "That&#39;s not the password." in html
    assert 'name="next" value="/people"' in html
    assert "checked" not in html


async def test_login_missing_password_field(client):
    resp = await client.post(
        "/login", data={"keep": "1"}, headers=_origin(client), allow_redirects=False
    )
    assert resp.status == 401


async def test_login_throttled_after_five_failures(client, clock):
    for _ in range(5):
        assert (await _login(client, "nope")).status == 401
    resp = await _login(client)  # even the right password is refused now
    assert resp.status == 429
    assert resp.headers["Retry-After"] == str(THROTTLE_WINDOW_S)
    assert COOKIE_NAME not in resp.cookies
    assert "Too many tries — try again in 15 min" in await resp.text()

    clock.advance(THROTTLE_WINDOW_S)
    assert (await _login(client)).status == 303


async def test_login_success_resets_throttle(client):
    for _ in range(4):
        await _login(client, "nope")
    assert (await _login(client)).status == 303
    for _ in range(4):
        assert (await _login(client, "nope")).status == 401
    assert (await _login(client)).status == 303


async def test_login_password_not_set(bare_client):
    resp = await _login(bare_client)
    assert resp.status == 503
    assert "The dashboard isn&#39;t set up yet." in await resp.text()
    assert COOKIE_NAME not in resp.cookies


# ─── /enter ──────────────────────────────────────────────────────────────


async def _enter(client, token: str | None):
    data = {} if token is None else {"t": token}
    return await client.post(
        "/enter", data=data, headers=_origin(client, "/enter"), allow_redirects=False
    )


async def test_enter_get_only_shows_a_confirm_page(client, settings_service):
    """Link previewers / prefetchers GET the link: that must not spend it."""
    token = await settings_service.issue_setup_token(actor_is_admin=True)
    for _ in range(3):
        resp = await client.get(f"/enter?t={token}", allow_redirects=False)
        assert resp.status == 200
        assert COOKIE_NAME not in resp.cookies
    html = await resp.text()
    assert 'method="post" action="/enter"' in html
    assert f'name="t" value="{token}"' in html
    assert "Sign in this tablet" in html
    assert await settings_service.consume_setup_token(token) is True


async def test_enter_confirm_page_escapes_token(client):
    resp = await client.get('/enter?t="><b>x', allow_redirects=False)
    html = await resp.text()
    assert "<b>x" not in html
    assert "&#34;&gt;&lt;b&gt;x" in html


async def test_enter_confirm_page_localised(client, settings_service):
    await settings_service.update(actor_is_admin=True, patch={"locale": "nl"})
    html = await (await client.get("/enter?t=abc")).text()
    assert "Deze tablet aanmelden" in html


async def test_enter_get_without_token_is_not_a_failure(client, auth):
    for _ in range(10):
        resp = await client.get("/enter", allow_redirects=False)
        assert resp.status == 303
        assert resp.headers["Location"] == "/login"
    assert auth.throttle_remaining("127.0.0.1") == 0
    assert (await _login(client)).status == 303


async def test_enter_post_valid_token_then_reuse(client, settings_service, auth):
    token = await settings_service.issue_setup_token(actor_is_admin=True)
    resp = await _enter(client, token)
    assert resp.status == 303
    assert resp.headers["Location"] == "/"
    morsel = resp.cookies[COOKIE_NAME]
    assert morsel["max-age"] == str(365 * 86400)
    claims = await auth.verify(morsel.value)
    assert claims is not None and claims.keep is True

    again = await _enter(client, token)
    assert again.status == 303
    assert again.headers["Location"] == "/login"
    assert COOKIE_NAME not in again.cookies


async def test_enter_post_without_token_is_not_a_failure(client, auth):
    for _ in range(10):
        resp = await _enter(client, None)
        assert resp.headers["Location"] == "/login"
    assert auth.throttle_remaining("127.0.0.1") == 0


async def test_enter_post_cross_origin_is_403(client, settings_service):
    token = await settings_service.issue_setup_token(actor_is_admin=True)
    resp = await client.post(
        "/enter",
        data={"t": token},
        headers={"Origin": "https://evil.example"},
        allow_redirects=False,
    )
    assert resp.status == 403
    assert await settings_service.consume_setup_token(token) is True


async def test_enter_failures_share_the_login_throttle(client, settings_service):
    for _ in range(5):
        assert (await _enter(client, "guess")).headers["Location"] == "/login"
    assert (await _login(client)).status == 429
    token = await settings_service.issue_setup_token(actor_is_admin=True)
    resp = await _enter(client, token)
    assert resp.status == 429
    assert resp.headers["Retry-After"] == str(THROTTLE_WINDOW_S)
    assert COOKIE_NAME not in resp.cookies
    assert "Too many tries — try again in 15 min" in await resp.text()
    # The throttled attempt didn't spend the token.
    assert await settings_service.consume_setup_token(token) is True


# ─── global brake ────────────────────────────────────────────────────────


async def test_global_brake_holds_back_every_client(client, auth):
    # Thirty-one failures from other addresses (each under its own limit).
    for n in range(31):
        assert auth.begin_attempt(f"10.0.{n}.1") == 0
    resp = await _login(client)
    assert resp.status == 429
    assert int(resp.headers["Retry-After"]) > 0
    assert COOKIE_NAME not in resp.cookies


async def test_global_brake_spares_signed_in_tablets(client, auth):
    for n in range(31):
        auth.begin_attempt(f"10.0.{n}.1")
    resp = await client.get("/", headers=await _cookie(auth))
    assert resp.status == 200


async def test_enter_failures_do_not_trip_the_global_brake(client, auth):
    # 40 setup-link failures from many addresses (8 each would lock a
    # client, so spread them out: the per-client limit is 5).
    for n in range(40):
        assert auth.begin_attempt(f"10.3.{n}.1", count_global=False) == 0
    for _ in range(4):
        assert (await _enter(client, "guess")).headers["Location"] == "/login"
    assert (await _login(client)).status == 303


async def test_global_brake_does_not_block_a_valid_setup_link(
    client, auth, settings_service
):
    for n in range(31):
        auth.begin_attempt(f"10.0.{n}.1")
    assert (await _login(client)).status == 429
    token = await settings_service.issue_setup_token(actor_is_admin=True)
    resp = await _enter(client, token)
    assert resp.status == 303
    assert resp.headers["Location"] == "/"
    assert COOKIE_NAME in resp.cookies


async def test_login_reads_settings_once(client):
    with patch.object(
        DashboardSettingsService,
        "get",
        autospec=True,
        side_effect=DashboardSettingsService.get,
    ) as get:
        assert (await _login(client, "nope")).status == 401
        assert get.call_count == 1
        get.reset_mock()
        assert (await client.get("/login")).status == 200
        assert get.call_count == 1


# ─── logout ──────────────────────────────────────────────────────────────


async def test_logout_clears_cookie(client, auth):
    headers = {**await _cookie(auth), **_origin(client, "/")}
    resp = await client.post("/logout", headers=headers, allow_redirects=False)
    assert resp.status == 303
    assert resp.headers["Location"] == "/login"
    morsel = resp.cookies[COOKIE_NAME]
    assert morsel.value == ""
    assert morsel["max-age"] == "0"


async def test_logout_without_session_still_clears(client):
    resp = await client.post(
        "/logout", headers=_origin(client, "/"), allow_redirects=False
    )
    assert resp.status == 303
    assert resp.headers["Location"] == "/login"


# ─── home placeholder ────────────────────────────────────────────────────


async def test_home_escapes_household_name(client, auth, household):
    household["name"] = "<script>alert(1)</script>"
    resp = await client.get("/", headers=await _cookie(auth))
    assert resp.status == 200
    html = await resp.text()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "Your dashboard is ready" in html
    assert 'action="/logout"' in html


async def test_parallel_guesses_cannot_outrun_the_throttle(client):
    """Each attempt counts before the (slow) password check, so a burst of
    concurrent guesses gets exactly five tries, not one per request."""
    results = await asyncio.gather(*(_login(client, "nope") for _ in range(10)))
    statuses = sorted(r.status for r in results)
    assert statuses == [401] * 5 + [429] * 5


# ─── Secure cookie behind a TLS reverse proxy ────────────────────────────


async def _https_address(settings_service) -> None:
    await settings_service.update(
        actor_is_admin=True, patch={"address": "https://dash.example.lan"}
    )


async def test_login_cookie_secure_behind_https_address(client, settings_service):
    await _https_address(settings_service)
    resp = await _login(client)
    assert resp.status == 303
    assert resp.cookies[COOKIE_NAME]["secure"] is True


async def test_enter_cookie_secure_behind_https_address(client, settings_service):
    await _https_address(settings_service)
    token = await settings_service.issue_setup_token(actor_is_admin=True)
    resp = await _enter(client, token)
    assert resp.cookies[COOKIE_NAME]["secure"] is True


async def test_logout_clears_the_secure_cookie(client, settings_service, auth):
    await _https_address(settings_service)
    headers = {**await _cookie(auth), **_origin(client, "/")}
    resp = await client.post("/logout", headers=headers, allow_redirects=False)
    assert resp.cookies[COOKIE_NAME]["secure"] is True


async def test_http_address_keeps_cookie_not_secure(client, settings_service):
    await settings_service.update(
        actor_is_admin=True, patch={"address": "http://dash.example.lan"}
    )
    resp = await _login(client)
    assert not resp.cookies[COOKIE_NAME]["secure"]


# ─── Page polish ─────────────────────────────────────────────────────────


async def test_password_input_autofocus_on_load_and_after_a_wrong_password(client):
    html = await (await client.get("/login")).text()
    assert 'type="password"' in html and "autofocus" in html
    resp = await _login(client, "nope")
    assert resp.status == 401
    html = await resp.text()
    assert 'name="password"' in html
    assert "autofocus" in html


async def test_enter_confirm_page_explains_itself(client, settings_service):
    html = await (await client.get("/enter?t=abc")).text()
    assert "This tablet will stay signed in to the household dashboard." in html
    await settings_service.update(actor_is_admin=True, patch={"locale": "de"})
    html = await (await client.get("/enter?t=abc")).text()
    assert "This tablet will stay signed in" not in html
