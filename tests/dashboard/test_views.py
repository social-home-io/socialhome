"""Tests for the dashboard views: login, setup link, logout, home."""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from dataclasses import replace
from html import unescape
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

import socialhome.i18n as i18n_pkg
from socialhome.dashboard.app import build_dashboard_app, build_dashboard_csp
from socialhome.dashboard.auth import (
    ACK_MAX,
    ACK_WINDOW_S,
    TILE_MAX,
    COOKIE_NAME,
    RENEW_AFTER_S,
    REVOKED_LOCKED,
    REVOKED_SIGNED_OUT,
    THROTTLE_MAX_FAILURES,
    THROTTLE_WINDOW_S,
    DashboardAuth,
)
from socialhome.dashboard.keys import AUTH_KEY, CLAIMS_KEY
from socialhome.dashboard.render import DashboardRenderer
from socialhome.dashboard.views import (
    _hostname,
    cookie_secure,
    image_type,
    safe_next,
)
from socialhome.services.dashboard_settings_service import DashboardSettingsService
from socialhome.services.map_tile_service import (
    MAP_ATTRIBUTION_TEXT,
    MapTileService,
    Tile,
    TileUnavailableError,
)
from socialhome.domain.household_dashboard import DashboardSticky
from socialhome.domain.theme import HouseholdTheme
from socialhome.i18n import Catalog
from socialhome.services.dashboard_display_service import DashboardDisplayService

from .conftest import PASSWORD, default_theme
from .factories import (
    ANNA,
    FOR_LEO,
    FOR_MIA,
    HERE,
    LEO,
    MIA,
    DINNER,
    MENTIONS,
    MESSAGES,
    MIA_SCHOOL,
    NOW,
    PASCAL,
    PEOPLE,
    SWIM,
    TODAY,
    TOMORROW,
    WORK,
    FakeDashboardService,
    at,
    event,
    leo_page,
    located,
    map_home,
    mention,
    message,
    person,
)

RENDERER = DashboardRenderer(
    Catalog.from_directory(Path(i18n_pkg.__file__).parent / "messages")
)


@pytest.fixture
def auth(settings_service, clock) -> DashboardAuth:
    return DashboardAuth(settings_service, clock=clock)


@pytest.fixture
def household():
    return {"name": "Home"}


class FakeTiles:
    """Stands in for :class:`MapTileService` (the main app's tile proxy)."""

    source_tag = "fake-tiles"

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int]] = []
        self.fail: Exception | None = None

    async def fetch(self, z: int, x: int, y: int) -> Tile:
        self.calls.append((z, x, y))
        if self.fail is not None:
            raise self.fail
        MapTileService._validate(z, x, y)
        return Tile(body=b"\x89PNG\r\n\x1a\ntile", content_type="image/png")


@pytest.fixture
def tiles() -> FakeTiles:
    return FakeTiles()


@pytest.fixture
async def bare_client(
    aiohttp_client, settings_service, auth, household, snapshots, tiles
):
    async def name() -> str:
        return household["name"]

    async def tz() -> str:
        return household.get("tz", "Europe/Berlin")

    async def theme() -> HouseholdTheme:
        if "theme" not in household:
            return HouseholdTheme()
        if isinstance(household["theme"], Exception):
            raise household["theme"]
        return HouseholdTheme(primary_color=household["theme"])

    app = build_dashboard_app(
        settings_service=settings_service,
        auth=auth,
        displays=DashboardDisplayService(settings_service, auth),
        renderer=RENDERER,
        household_name=name,
        household_tz=tz,
        household_theme=theme,
        snapshots=snapshots,
        tiles=tiles,
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


async def _cookie(auth: DashboardAuth, *, locked: bool = False) -> dict[str, str]:
    return {"Cookie": f"{COOKIE_NAME}={await auth.issue(True, locked=locked)}"}


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


# ─── home ────────────────────────────────────────────────────────────────


async def test_home_escapes_household_name(client, auth, household):
    household["name"] = "<script>alert(1)</script>"
    resp = await client.get("/", headers=await _cookie(auth))
    assert resp.status == 200
    html = await resp.text()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert 'class="dash-home"' in html


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


#: What a browser sends through the TLS proxy (which keeps ``Host``).
_PROXIED = {
    "Host": "dash.example.lan",
    "Origin": "https://dash.example.lan",
    "Referer": "https://dash.example.lan/login",
}


async def test_login_cookie_secure_behind_https_address(client, settings_service):
    await _https_address(settings_service)
    resp = await client.post(
        "/login",
        data={"password": PASSWORD, "keep": "1"},
        headers=_PROXIED,
        allow_redirects=False,
    )
    assert resp.status == 303
    assert resp.cookies[COOKIE_NAME]["secure"] is True


async def test_https_address_lan_login_cookie_not_secure(client, settings_service):
    """A tablet that opens the LAN IP directly is on plain HTTP: a
    ``Secure`` cookie there would be dropped and login would loop."""
    await _https_address(settings_service)
    resp = await _login(client)
    assert resp.status == 303
    assert not resp.cookies[COOKIE_NAME]["secure"]


async def test_enter_cookie_secure_behind_https_address(client, settings_service):
    await _https_address(settings_service)
    token = await settings_service.issue_setup_token(actor_is_admin=True)
    resp = await client.post(
        "/enter",
        data={"t": token},
        headers={**_PROXIED, "Referer": "https://dash.example.lan/enter"},
        allow_redirects=False,
    )
    assert resp.cookies[COOKIE_NAME]["secure"] is True


async def test_logout_clears_the_secure_cookie(client, settings_service, auth):
    await _https_address(settings_service)
    headers = {
        **await _cookie(auth),
        **_PROXIED,
        "Referer": "https://dash.example.lan/",
    }
    resp = await client.post("/logout", headers=headers, allow_redirects=False)
    assert resp.cookies[COOKIE_NAME]["secure"] is True


async def test_tls_request_cookie_always_secure():
    """A request that arrived over TLS needs no address to be ``Secure``."""
    request = SimpleNamespace(secure=True, host="192.168.1.20:8100", app={})
    assert await cookie_secure(request) is True


@pytest.mark.parametrize(
    ("netloc", "expected"),
    [
        ("Dash.Example.LAN:8443", "dash.example.lan"),
        ("dash.example.lan.", "dash.example.lan"),
        ("[::1]:8100", "::1"),
        ("[::1", ""),  # malformed IPv6 netloc → no name, never Secure
    ],
)
def test_hostname(netloc, expected):
    assert _hostname(netloc) == expected


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


# ─── Privacy lock ────────────────────────────────────────────────────────

PIN = "482913"
#: A member's name the lock page must never show (a person's page id).
MEMBER = "Alice-Wonderland"
WRONG = "/?pin=wrong"
WAIT = "/?pin=wait"


@pytest.fixture
async def pin_client(client, settings_service):
    await settings_service.set_pin(actor_is_admin=True, pin=PIN)
    return client


async def _locked(auth) -> dict[str, str]:
    return await _cookie(auth, locked=True)


def _jar(resp) -> dict[str, str]:
    return {"Cookie": f"{COOKIE_NAME}={resp.cookies[COOKIE_NAME].value}"}


async def _unlock(client, headers, pin=PIN):
    return await client.post(
        "/unlock",
        data={"pin": pin},
        headers={**headers, **_origin(client, "/")},
        allow_redirects=False,
    )


async def _lock(client, headers):
    return await client.post(
        "/lock",
        headers={**headers, **_origin(client, "/")},
        allow_redirects=False,
    )


async def _claims(auth, resp):
    return await auth.verify(resp.cookies[COOKIE_NAME].value)


def _assert_strict_csp(resp, html: str) -> None:
    """Served under the default policy and needing nothing beyond it."""
    assert resp.headers["Content-Security-Policy"] == build_dashboard_csp()
    assert "<style" not in html
    assert re.search(r"\sstyle=", html) is None
    assert re.search(r"\son[a-z]+=", html) is None
    for tag in re.findall(r"<script\b[^>]*>", html):
        assert 'src="/static/' in tag, tag
    assert "javascript:" not in html


def _assert_nothing_personal(html: str) -> None:
    assert MEMBER not in html
    assert "/avatar/" not in html
    assert "<img" not in html


async def test_home_has_no_lock_button_without_a_pin(client, auth):
    html = await (await client.get("/", headers=await _cookie(auth))).text()
    assert 'action="/lock"' not in html


async def test_home_has_the_lock_button_with_a_pin(pin_client, auth):
    html = await (await pin_client.get("/", headers=await _cookie(auth))).text()
    assert 'action="/lock"' in html
    assert 'aria-label="Lock screen"' in html
    assert "dash-lock-button" in html


async def test_lock_without_a_pin_is_409(client, auth):
    resp = await _lock(client, await _cookie(auth))
    assert resp.status == 409
    assert COOKIE_NAME not in resp.cookies


async def test_lock_without_a_session_is_401(pin_client):
    resp = await pin_client.post(
        "/lock", headers=_origin(pin_client, "/"), allow_redirects=False
    )
    assert resp.status == 401


async def test_lock_from_a_foreign_origin_is_403(pin_client, auth):
    resp = await pin_client.post(
        "/lock",
        headers={**await _cookie(auth), "Origin": "http://evil.example"},
        allow_redirects=False,
    )
    assert resp.status == 403


@pytest.mark.parametrize("path", ["/lock", "/unlock"])
@pytest.mark.parametrize("locked", [False, True])
async def test_get_lock_paths_go_home(pin_client, auth, path, locked):
    """Nothing to re-POST: a reload or a typed URL lands on ``/``."""
    resp = await pin_client.get(
        path, headers=await _cookie(auth, locked=locked), allow_redirects=False
    )
    assert resp.status == 303
    assert resp.headers["Location"] == "/"


@pytest.mark.parametrize("keep", [True, False])
async def test_lock_reissues_a_locked_cookie(pin_client, auth, keep):
    headers = {"Cookie": f"{COOKIE_NAME}={await auth.issue(keep)}"}
    resp = await _lock(pin_client, headers)
    assert resp.status == 303
    assert resp.headers["Location"] == "/"
    claims = await _claims(auth, resp)
    assert claims is not None and claims.locked is True and claims.keep is keep
    morsel = resp.cookies[COOKIE_NAME]
    assert morsel["httponly"] is True and morsel["samesite"] == "Strict"
    assert morsel["max-age"] == ("31536000" if keep else "")


async def test_lock_on_a_locked_session_changes_nothing(pin_client, auth):
    """N1: no new cookie (so no new session id, no fresh PIN bucket) and
    no revocation entry."""
    resp = await _lock(pin_client, await _locked(auth))
    assert resp.status == 303
    assert resp.headers["Location"] == "/"
    assert COOKIE_NAME not in resp.cookies
    assert auth.revoked_count == 0


async def test_lock_and_unlock_start_new_sessions(pin_client, auth):
    unlocked = await auth.verify((await _cookie(auth))["Cookie"].split("=", 1)[1])
    lock = await _lock(pin_client, await _cookie(auth))
    locked = await _claims(auth, lock)
    unlock = await _unlock(pin_client, _jar(lock))
    after = await _claims(auth, unlock)
    ids = {unlocked.session_id, locked.session_id, after.session_id}
    assert len(ids) == 3


async def test_renewal_keeps_the_session_id(pin_client, auth, clock):
    for locked in (False, True):
        raw = await auth.issue(True, locked=locked)
        clock.advance(RENEW_AFTER_S + 1)
        resp = await pin_client.get("/", headers={"Cookie": f"{COOKIE_NAME}={raw}"})
        renewed = await _claims(auth, resp)
        assert renewed.session_id == (await auth.verify(raw)).session_id
        assert renewed.locked is locked


async def test_lock_revokes_every_copy_of_the_unlocked_session(pin_client, auth, clock):
    """Locking a renewed cookie also locks the older copy it came from."""
    older = await auth.issue(True)
    clock.advance(RENEW_AFTER_S + 1)
    renewed = await pin_client.get("/", headers={"Cookie": f"{COOKIE_NAME}={older}"})
    await _lock(pin_client, _jar(renewed))
    resp = await pin_client.get("/", headers={"Cookie": f"{COOKIE_NAME}={older}"})
    assert "data-lock" in await resp.text()


async def test_chaining_lock_cannot_reset_the_pin_budget(pin_client, auth):
    """N1 regression (attack harness): re-POSTing /lock every five guesses
    used to mint a fresh cookie — and a fresh PIN bucket — forever."""
    current = await _locked(auth)
    for guess in range(THROTTLE_MAX_FAILURES * 3):
        if guess and guess % THROTTLE_MAX_FAILURES == 0:
            lock = await _lock(pin_client, current)
            if COOKIE_NAME in lock.cookies:
                current = _jar(lock)
        resp = await _unlock(pin_client, current, pin=f"{guess:06d}")
        if resp.status == 401 or "signed_out" in resp.headers.get("Location", ""):
            break
    assert guess == THROTTLE_MAX_FAILURES  # the sixth try signs it out
    assert auth.throttle_key_count == 1


async def test_a_locked_session_cannot_grow_the_maps(pin_client, auth):
    """Only a password holder (an unlocked session) adds revocations, so
    whoever holds just a locked cookie can't flood the revocation map
    (evicting other sign-outs) or the throttle table."""
    headers = await _locked(auth)
    for _ in range(20):
        lock = await _lock(pin_client, headers)
        if COOKIE_NAME in lock.cookies:  # follow whatever it hands back
            headers = _jar(lock)
    for _ in range(THROTTLE_MAX_FAILURES + 3):
        await _unlock(pin_client, headers, pin="000000")
    for _ in range(5):
        await _lock(pin_client, headers)
    assert auth.revoked_count == 1  # its own sign-out
    assert auth.throttle_key_count == 1


async def test_renewed_lock_shares_its_pin_bucket_and_its_sign_out(
    pin_client, auth, clock
):
    """N1b: renewing a locked cookie keeps the session id — same bucket,
    and the sign-out kills both copies."""
    first = await _locked(auth)
    clock.advance(RENEW_AFTER_S + 1)  # still inside the one-year cookie
    renewed = _jar(await pin_client.get("/", headers=first))
    for _ in range(THROTTLE_MAX_FAILURES - 1):
        await _unlock(pin_client, first, pin="000000")
    assert (await _unlock(pin_client, renewed, pin="000000")).headers[
        "Location"
    ] == WRONG
    resp = await _unlock(pin_client, renewed)
    assert resp.headers["Location"] == "/login?signed_out=1"
    for copy in (first, renewed):
        page = await pin_client.get("/", headers=copy, allow_redirects=False)
        assert page.status == 302


async def test_rotating_client_addresses_share_one_pin_bucket(pin_client, auth):
    """N2: a fresh IPv6 /64 per guess doesn't buy a fresh bucket."""
    headers = await _locked(auth)
    addresses = iter(f"2001:db8:{n}::/64" for n in range(100))
    with patch(
        "socialhome.dashboard.views.client_bucket",
        side_effect=lambda _remote: next(addresses),
    ):
        for _ in range(THROTTLE_MAX_FAILURES):
            await _unlock(pin_client, headers, pin="000000")
        resp = await _unlock(pin_client, headers)
    assert resp.headers["Location"] == "/login?signed_out=1"


async def test_lock_then_reload_shows_only_the_lock_screen(pin_client, auth, household):
    household["name"] = "The Smiths"
    lock = await _lock(pin_client, await _cookie(auth))
    for _ in range(2):  # a reload keeps it
        resp = await pin_client.get("/", headers=_jar(lock), allow_redirects=False)
        assert resp.status == 200
        html = await resp.text()
        assert "data-lock" in html
        assert "The Smiths" in html
        assert 'action="/unlock"' in html
        assert 'class="dash-home"' not in html
        assert 'action="/logout"' not in html
        assert 'action="/lock"' not in html
        _assert_nothing_personal(html)
        _assert_strict_csp(resp, html)


# F1 — a stale unlocked cookie can't lift the lock.


async def test_pre_lock_cookie_counts_as_locked_after_lock(pin_client, auth):
    """A late response re-sending the pre-lock cookie lands a revoked
    ("locked") cookie: it shows the lock screen, it doesn't sign out."""
    before = await _cookie(auth)
    assert (await _lock(pin_client, before)).status == 303
    resp = await pin_client.get("/", headers=before, allow_redirects=False)
    assert resp.status == 200
    assert "data-lock" in await resp.text()
    # …and it can be unlocked with the PIN like any locked cookie.
    unlock = await _unlock(pin_client, before)
    assert unlock.headers["Location"] == "/"
    assert (await _claims(auth, unlock)).locked is False


async def test_old_pre_lock_cookie_renews_locked_after_lock(pin_client, auth, clock):
    before = await _cookie(auth)
    await _lock(pin_client, before)
    clock.advance(RENEW_AFTER_S + 1)
    resp = await pin_client.get("/", headers=before)
    assert (await _claims(auth, resp)).locked is True


async def test_no_renewal_on_a_fragment(pin_client, auth, clock):
    headers = await _cookie(auth)
    clock.advance(RENEW_AFTER_S + 1)
    resp = await pin_client.get("/fragment/x", headers=headers)
    assert COOKIE_NAME not in resp.cookies


async def test_no_renewal_on_a_post(client, auth, clock):
    headers = await _cookie(auth)
    clock.advance(RENEW_AFTER_S + 1)
    resp = await _lock(client, headers)  # no PIN → 409, handled, no cookie
    assert resp.status == 409
    assert COOKIE_NAME not in resp.cookies


@pytest.fixture
async def race_client(aiohttp_client, settings_service, auth):
    """A dashboard whose Home render revokes the very cookie that reached
    it — what a ``/lock`` racing a slow page load does."""
    await settings_service.set_password(actor_is_admin=True, password=PASSWORD)
    await settings_service.set_pin(actor_is_admin=True, pin=PIN)

    async def name() -> str:
        return "Home"

    async def tz() -> str:
        return "UTC"

    class RacingSnapshots(FakeDashboardService):
        why: str = ""

        async def home(self, *, now=None):
            request = self.request
            request.app[AUTH_KEY].revoke(request[CLAIMS_KEY], self.why)
            return await super().home(now=now)

    snaps = RacingSnapshots()
    app = build_dashboard_app(
        settings_service=settings_service,
        auth=auth,
        displays=DashboardDisplayService(settings_service, auth),
        renderer=RENDERER,
        household_name=name,
        household_tz=tz,
        household_theme=default_theme,
        snapshots=snaps,
    )

    @web.middleware
    async def remember(request: web.Request, handler):
        snaps.request = request
        return await handler(request)

    app.middlewares.append(remember)
    client = await aiohttp_client(app, cookie_jar=aiohttp.DummyCookieJar())
    return client, snaps


async def test_renewal_after_a_racing_lock_issues_a_locked_cookie(
    race_client, auth, clock
):
    client, snaps = race_client
    snaps.why = REVOKED_LOCKED
    headers = await _cookie(auth)
    clock.advance(RENEW_AFTER_S + 1)
    resp = await client.get("/", headers=headers)
    assert resp.status == 200
    assert (await _claims(auth, resp)).locked is True


async def test_renewal_after_a_racing_sign_out_issues_nothing(race_client, auth, clock):
    client, snaps = race_client
    snaps.why = REVOKED_SIGNED_OUT
    headers = await _cookie(auth)
    clock.advance(RENEW_AFTER_S + 1)
    resp = await client.get("/", headers=headers)
    assert COOKIE_NAME not in resp.cookies


# The lock page.


async def test_lock_page_form_works_without_js(pin_client, auth):
    html = await (await pin_client.get("/", headers=await _locked(auth))).text()
    form = re.search(r'<form[^>]*action="/unlock"[^>]*>.*?</form>', html, re.S)
    assert form is not None
    assert 'method="post"' in form.group(0)
    field = re.search(r'<input[^>]*name="pin"[^>]*>', form.group(0)).group(0)
    for attr in (
        'inputmode="numeric"',
        'pattern="[0-9]*"',
        'autocomplete="off"',
        'maxlength="6"',
    ):
        assert attr in field
    assert 'type="submit"' in form.group(0)
    assert '<script src="/static/lock.js" defer></script>' in html
    # The no-JS refresh is a GET of the plain lock screen — never a re-POST
    # and never the one-off error flag.
    assert '<meta http-equiv="refresh" content="60;url=/">' in html


async def test_lock_page_shows_household_clock_and_date(pin_client, auth):
    # Fixed clock 2027-01-15 08:00 UTC → 09:00 in Berlin.
    html = await (await pin_client.get("/", headers=await _locked(auth))).text()
    assert ">09:00<" in html
    assert "Friday, January 15" in html
    assert 'data-tz="Europe/Berlin"' in html
    assert 'data-day="2027-01-15"' in html


async def test_lock_page_in_the_dashboard_locale(pin_client, auth, settings_service):
    await settings_service.update(actor_is_admin=True, patch={"locale": "de"})
    html = await (await pin_client.get("/", headers=await _locked(auth))).text()
    assert '<html lang="de"' in html
    assert "Freitag, 15. Januar" in html
    assert "Entsperren" in html


async def test_lock_page_with_an_unknown_timezone_falls_back_to_utc(
    pin_client, auth, household
):
    household["tz"] = "Not/AZone"
    html = await (await pin_client.get("/", headers=await _locked(auth))).text()
    assert ">08:00<" in html
    assert 'data-tz="UTC"' in html


async def test_locked_person_page_shows_the_lock_screen(pin_client, auth):
    resp = await pin_client.get(
        f"/p/{MEMBER}", headers=await _locked(auth), allow_redirects=False
    )
    assert resp.status == 200
    html = await resp.text()
    assert "data-lock" in html
    _assert_nothing_personal(html)


async def test_locked_head_is_the_lock_screen(pin_client, auth):
    resp = await pin_client.head("/", headers=await _locked(auth))
    assert resp.status == 200


async def test_locked_avatar_is_423(pin_client, auth):
    resp = await pin_client.get(
        f"/avatar/{MEMBER}", headers=await _locked(auth), allow_redirects=False
    )
    assert resp.status == 423
    _assert_nothing_personal(await resp.text())


async def test_locked_fragment_is_a_marker(pin_client, auth):
    resp = await pin_client.get(
        f"/fragment/person/{MEMBER}", headers=await _locked(auth)
    )
    assert resp.status == 200
    assert resp.headers["X-Dashboard-Locked"] == "1"
    assert await resp.text() == '<div data-dashboard-locked="1"></div>'


async def test_locked_other_post_is_423(pin_client, auth):
    resp = await pin_client.post(
        "/somewhere",
        headers={**await _locked(auth), **_origin(pin_client, "/")},
        allow_redirects=False,
    )
    assert resp.status == 423


async def test_locked_tablet_reaches_public_paths(pin_client, auth):
    headers = await _locked(auth)
    assert (await pin_client.get("/healthz", headers=headers)).status == 200
    assert (await pin_client.get("/static/lock.js", headers=headers)).status == 200
    resp = await pin_client.post(
        "/logout",
        headers={**headers, **_origin(pin_client, "/")},
        allow_redirects=False,
    )
    assert resp.status == 303
    assert resp.headers["Location"] == "/login"


async def test_lock_js_is_served_as_javascript(pin_client):
    resp = await pin_client.get("/static/lock.js")
    assert resp.status == 200
    assert resp.content_type == "text/javascript"


# F6 — no stale page from the back/forward cache.


async def test_dashboard_js_reloads_bfcache_restores(pin_client):
    resp = await pin_client.get("/static/dashboard.js")
    assert resp.status == 200
    assert resp.content_type == "text/javascript"
    js = await resp.text()
    assert "pageshow" in js and "persisted" in js and "reload" in js


def test_dashboard_js_speaks_only_on_a_tap():
    """Messages are read aloud only from the click handler — never on
    load, on a refresh or a timer — one at a time, and the affordance
    depends on a voice for the page language."""
    js = RENDERER.static_asset("dashboard.js").body.decode()
    assert js.count(".speak(") == 1
    click = js[js.index('addEventListener("click", function (event) {\n    var el') :]
    click = click[: click.index("\n  });")]
    assert "synth.speak(utterance)" in click and "synth.cancel()" in click
    assert "voice === null" in click  # no voice → a tap does nothing
    assert 'getAttribute("data-say")' in click
    # Leaving the page stops it (and forgets it, for a back/forward
    # restore); a swap that drops the message stops it.
    hide = js[js.index('addEventListener("pagehide"') :]
    assert "stopSpeaking()" in hide[: hide.index("});")]
    assert "sayAfterSwap();" in js[js.index("function swap(") :]


def test_dashboard_js_speaks_only_with_an_on_device_voice():
    """Message text never leaves the tablet: only a local voice for the
    page language counts (Chrome's "Google …" voices are remote); none →
    voice stays null → no 🔊, no description, a tap does nothing. A voice
    that can't be assigned means not speaking (the engine could otherwise
    pick a remote default)."""
    js = RENDERER.static_asset("dashboard.js").body.decode()
    find = js[js.index("function findVoice()") :]
    find = find[: find.index("\n  }\n")]
    assert "v.localService !== true" in find
    assert "return null" in find
    click = js[js.index('addEventListener("click", function (event) {\n    var el') :]
    click = click[: click.index("\n  });")]
    assign = click[click.index("utterance.voice = voice") :]
    assert "catch" in assign and "return;" in assign[: assign.index("synth.speak(")]
    assert "utterance.voice !== voice" in click
    # Voices that load late are picked up without addEventListener too,
    # and once more after a second.
    assert "synth.onvoiceschanged = updateVoice" in js
    assert "window.setTimeout(updateVoice, 1000)" in js


def test_dashboard_js_mention_tone_is_on_device_once_and_quiet_at_night():
    """The mention tone: synthesised (WebAudio, nothing fetched), soft
    (gain never above 0.12), once per mention per tablet (sessionStorage,
    every access in try/catch), never at night or with the admin's
    switch off, silent when autoplay is blocked."""
    js = RENDERER.static_asset("dashboard.js").body.decode()
    assert js.count("chime();") == 1
    sync = js[js.index("function syncMentions()") :]
    sync = sync[: sync.index("\n  }\n")]
    assert "chime();" in sync
    assert 'getAttribute("data-mention-sound") === "1"' in sync
    assert '!body.classList.contains("dash-night")' in sync
    assert "seen.indexOf(k) < 0" in sync and "saveSeen()" in sync
    assert "syncMentions();" in js[js.index("function swap(") :]
    for fn in ("function loadSeen()", "function saveSeen()"):
        body = js[js.index(fn) :]
        body = body[: body.index("\n  }\n")]
        assert "try {" in body and "catch (e)" in body, fn
    tone = js[js.index("function tone(ctx)") :]
    tone = tone[: tone.index("\n  }\n")]
    assert 'osc.type = "sine"' in tone and "osc.stop(" in tone
    gains = [float(g) for g in re.findall(r"linearRampToValueAtTime\(([\d.]+)", tone)]
    assert gains and max(gains) <= 0.12
    assert "new Audio(" not in js and ".mp3" not in js and ".ogg" not in js
    # A resume that only lands later (on some tap) plays nothing.
    chime = js[js.index("function chime()") :]
    chime = chime[: chime.index("\n  }\n")]
    assert "TONE_LATE_MS" in chime


def test_dashboard_js_says_which_message_plays():
    """The Messages tab row is a toggle: aria-pressed follows the speaking
    row (a second tap stops it), and a polite live region names whose
    message is read — from the row's own (server-translated) sentence."""
    js = RENDERER.static_asset("dashboard.js").body.decode()
    mark = js[js.index("function markSay()") :]
    mark = mark[: mark.index("\n  }\n")]
    assert 'hasAttribute("aria-pressed")' in mark
    assert 'setAttribute("aria-pressed", on ? "true" : "false")' in mark
    assert 'getAttribute("data-say-playing")' in mark
    assert "sayStatus(" in mark
    status = js[js.index("function sayStatus(") :]
    status = status[: status.index("\n  }\n")]
    assert 'querySelector("[data-say-status]")' in status
    # Written only on a change: a refresh never re-announces it.
    assert "el.textContent !== text" in status


def test_dashboard_js_posts_got_it_same_origin():
    js = RENDERER.static_asset("dashboard.js").body.decode()
    submit = js[js.index('addEventListener("submit"') :]
    submit = submit[: submit.index("\n  });")]
    assert 'hasAttribute("data-ack")' in submit
    assert 'method: "POST"' in submit and 'credentials: "same-origin"' in submit
    assert 'redirect: "manual"' in submit
    assert "event.preventDefault()" in submit and "refresh();" in submit


def test_dashboard_js_never_leaves_the_page_while_offline():
    """Q4: the midnight reload and the idle return probe the server first
    (offline they'd swap the last page for the browser's error page)."""
    js = RENDERER.static_asset("dashboard.js").body.decode()
    assert "goWhenReachable(window.location.pathname, fragmentUrl)" in js
    assert 'goWhenReachable("/", HOME_PROBE)' in js
    # A gone person (404 on the probe) goes Home, never a script-less 404 page.
    assert "res.status === 404" in js and 'go("/")' in js
    tick = js[js.index("function tick()") : js.index("tick();")]
    assert "location.reload" not in tick and "go(" not in tick
    idle = js[js.index("var idleDue") : js.index("var armIdle")]
    assert 'go("/")' not in idle


@pytest.mark.parametrize("locked", [False, True])
async def test_pages_load_dashboard_js(pin_client, auth, locked):
    html = await (
        await pin_client.get("/", headers=await _cookie(auth, locked=locked))
    ).text()
    assert '<script src="/static/dashboard.js" defer></script>' in html


# Unlocking.


async def test_correct_pin_unlocks(pin_client, auth):
    resp = await _unlock(pin_client, await _locked(auth))
    assert resp.status == 303
    assert resp.headers["Location"] == "/"
    claims = await _claims(auth, resp)
    assert claims is not None and claims.locked is False and claims.keep is True
    home = await pin_client.get("/", headers=_jar(resp))
    assert 'class="dash-home"' in await home.text()


async def test_wrong_pin_redirects_to_the_lock_screen_with_a_flag(pin_client, auth):
    headers = await _locked(auth)
    resp = await _unlock(pin_client, headers, pin="000000")
    assert resp.status == 303
    assert resp.headers["Location"] == WRONG
    assert COOKIE_NAME not in resp.cookies
    page = await pin_client.get(WRONG, headers=headers)
    assert page.status == 200
    html = await page.text()
    assert "Wrong PIN" in html
    assert 'role="alert"' in html
    assert re.search(r"<details[^>]*\sopen", html) is not None
    _assert_nothing_personal(html)
    _assert_strict_csp(page, html)


async def test_lock_page_keypad_is_closed_until_asked(pin_client, auth):
    html = await (await pin_client.get("/", headers=await _locked(auth))).text()
    assert re.search(r"<details[^>]*\sopen", html) is None
    assert "Wrong PIN" not in html


@pytest.mark.parametrize("flag", ["wrong", "wait", "x"])
async def test_pin_flags_are_ignored_while_unlocked(pin_client, auth, flag):
    resp = await pin_client.get(f"/?pin={flag}", headers=await _cookie(auth))
    assert resp.status == 200
    html = await resp.text()
    assert 'class="dash-home"' in html
    assert "Wrong PIN" not in html and "Too many tries" not in html


async def test_unknown_pin_flag_is_the_plain_lock_screen(pin_client, auth):
    resp = await pin_client.get("/?pin=x", headers=await _locked(auth))
    assert resp.status == 200
    html = await resp.text()
    assert "Wrong PIN" not in html and "Too many tries" not in html


@pytest.mark.parametrize("data", [{}, {"pin": ""}, {"pin": "x" * 5000}])
async def test_malformed_pin_counts_as_wrong(pin_client, auth, data):
    resp = await pin_client.post(
        "/unlock",
        data=data,
        headers={**await _locked(auth), **_origin(pin_client, "/")},
        allow_redirects=False,
    )
    assert resp.headers["Location"] == WRONG


async def test_five_wrong_pins_then_the_tablet_is_signed_out(pin_client, auth):
    headers = await _locked(auth)
    for _ in range(THROTTLE_MAX_FAILURES):
        assert (await _unlock(pin_client, headers, pin="000000")).headers[
            "Location"
        ] == WRONG
    resp = await _unlock(pin_client, headers)  # even the right PIN now
    assert resp.status == 303
    assert resp.headers["Location"] == "/login?signed_out=1"
    morsel = resp.cookies[COOKIE_NAME]
    assert morsel.value == "" and morsel["max-age"] == "0"
    login = await pin_client.get("/login?signed_out=1")
    html = await login.text()
    assert "Signed out after too many wrong PINs" in html
    assert 'name="password"' in html


async def test_signed_out_cookie_is_dead_even_when_replayed(pin_client, auth):
    """F5: a copied locked cookie can't keep guessing after the sign-out."""
    headers = await _locked(auth)
    for _ in range(THROTTLE_MAX_FAILURES + 1):
        await _unlock(pin_client, headers, pin="000000")
    assert (await _unlock(pin_client, headers)).status == 401
    page = await pin_client.get("/", headers=headers, allow_redirects=False)
    assert page.status == 302
    assert page.headers["Location"].startswith("/login")


async def test_pin_lockout_does_not_block_the_password(pin_client, auth):
    headers = await _locked(auth)
    for _ in range(THROTTLE_MAX_FAILURES + 1):
        await _unlock(pin_client, headers, pin="000000")
    login = await _login(pin_client)
    assert login.status == 303
    # The new session has a fresh PIN budget once locked again.
    lock = await _lock(pin_client, _jar(login))
    assert (await _unlock(pin_client, _jar(lock))).headers["Location"] == "/"


async def test_one_tablets_wrong_pins_spare_another_on_the_same_address(
    pin_client, auth
):
    """F3: behind a TLS proxy every tablet has the same client address."""
    tablet_a = await _locked(auth)
    tablet_b = await _locked(auth)
    for _ in range(THROTTLE_MAX_FAILURES + 1):
        await _unlock(pin_client, tablet_a, pin="000000")
    assert (await _unlock(pin_client, tablet_a)).status == 401  # signed out
    for _ in range(THROTTLE_MAX_FAILURES - 1):
        assert (await _unlock(pin_client, tablet_b, pin="000000")).headers[
            "Location"
        ] == WRONG
    assert (await _unlock(pin_client, tablet_b)).headers["Location"] == "/"


@pytest.fixture
async def tiny_client(aiohttp_client, settings_service, clock):
    """A dashboard whose PIN throttle table holds one lock."""
    await settings_service.set_password(actor_is_admin=True, password=PASSWORD)
    await settings_service.set_pin(actor_is_admin=True, pin=PIN)
    auth = DashboardAuth(settings_service, clock=clock, max_pin_keys=1)

    async def name() -> str:
        return "Home"

    async def tz() -> str:
        return "UTC"

    app = build_dashboard_app(
        settings_service=settings_service,
        auth=auth,
        displays=DashboardDisplayService(settings_service, auth),
        renderer=RENDERER,
        household_name=name,
        household_tz=tz,
        household_theme=default_theme,
        snapshots=FakeDashboardService(),
    )
    client = await aiohttp_client(app, cookie_jar=aiohttp.DummyCookieJar())
    return client, auth


async def test_full_throttle_table_holds_back_but_never_signs_out(tiny_client, clock):
    """F2: only this tablet's own wrong PINs sign it out."""
    client, auth = tiny_client
    # Another tablet's lock holds the only PIN slot (with live failures,
    # so it can't be evicted for this one).
    for _ in range(THROTTLE_MAX_FAILURES):
        auth.begin_attempt("pin:someone-else", count_global=False)
    headers = await _locked(auth)
    resp = await _unlock(client, headers)
    assert resp.status == 303
    assert resp.headers["Location"] == WAIT
    assert int(resp.headers["Retry-After"]) > 0
    assert COOKIE_NAME not in resp.cookies
    page = await client.get(WAIT, headers=headers)
    assert page.status == 429
    assert int(page.headers["Retry-After"]) > 0
    html = await page.text()
    assert "data-lock" in html
    assert "Too many tries" in html
    _assert_strict_csp(page, html)
    # The cookie still works once the table has room again.
    clock.advance(THROTTLE_WINDOW_S)
    assert (await _unlock(client, headers)).headers["Location"] == "/"


async def test_wait_flag_without_a_wait_is_the_plain_lock_screen(pin_client, auth):
    resp = await pin_client.get(WAIT, headers=await _locked(auth))
    assert resp.status == 200
    assert "Too many tries" not in await resp.text()


async def test_pin_throttle_expires(pin_client, auth, clock):
    headers = await _locked(auth)
    for _ in range(THROTTLE_MAX_FAILURES):
        await _unlock(pin_client, headers, pin="000000")
    clock.advance(THROTTLE_WINDOW_S)
    assert (await _unlock(pin_client, headers)).headers["Location"] == "/"


async def test_correct_pin_resets_the_pin_failures(pin_client, auth):
    headers = await _locked(auth)
    for _ in range(THROTTLE_MAX_FAILURES - 1):
        await _unlock(pin_client, headers, pin="000000")
    assert (await _unlock(pin_client, headers)).headers["Location"] == "/"
    for _ in range(THROTTLE_MAX_FAILURES - 1):
        assert (await _unlock(pin_client, headers, pin="000000")).headers[
            "Location"
        ] == WRONG
    assert (await _unlock(pin_client, headers)).headers["Location"] == "/"


async def test_pin_failures_do_not_trip_the_global_brake(pin_client, auth):
    headers = await _locked(auth)
    for _ in range(THROTTLE_MAX_FAILURES):
        await _unlock(pin_client, headers, pin="000000")
    assert auth.throttle_remaining("elsewhere") == 0
    assert auth.global_failure_count == 0


async def test_parallel_pin_guesses_cannot_outrun_the_throttle(pin_client, auth):
    headers = await _locked(auth)
    results = await asyncio.gather(
        *(_unlock(pin_client, headers, pin="000000") for _ in range(8))
    )
    wrong = [r for r in results if r.headers.get("Location") == WRONG]
    out = [r for r in results if r.headers.get("Location") == "/login?signed_out=1"]
    # Past the limit: the first sign-out revokes the cookie, so guesses
    # still in flight behind it find no session at all.
    dead = [r for r in results if r.status == 401]
    assert len(wrong) == THROTTLE_MAX_FAILURES
    assert len(out) >= 1
    assert len(wrong) + len(out) + len(dead) == 8


async def test_unlock_an_unlocked_session_redirects_home(pin_client, auth):
    resp = await _unlock(pin_client, await _cookie(auth), pin="000000")
    assert resp.status == 303
    assert resp.headers["Location"] == "/"
    assert COOKIE_NAME not in resp.cookies
    # …and is not a PIN guess.
    assert auth.throttle_key_count == 0


async def test_unlock_without_a_session_is_401(pin_client):
    resp = await pin_client.post(
        "/unlock",
        data={"pin": PIN},
        headers=_origin(pin_client, "/"),
        allow_redirects=False,
    )
    assert resp.status == 401


async def test_unlock_from_a_foreign_origin_is_403(pin_client, auth):
    resp = await pin_client.post(
        "/unlock",
        data={"pin": PIN},
        headers={
            **await _locked(auth),
            "Origin": "http://evil.example",
            "Referer": "http://evil.example/",
        },
        allow_redirects=False,
    )
    assert resp.status == 403
    assert COOKIE_NAME not in resp.cookies


async def test_deleted_pin_treats_locked_tablets_as_unlocked(
    pin_client, auth, settings_service
):
    headers = await _locked(auth)
    revoked = await _cookie(auth)
    await _lock(pin_client, revoked)  # a revoked-at-lock cookie too
    await settings_service.delete_pin(actor_is_admin=True)
    for cookie in (headers, revoked):
        resp = await pin_client.get("/", headers=cookie)
        assert resp.status == 200
        html = await resp.text()
        assert 'class="dash-home"' in html
        assert "data-lock" not in html
    # /unlock has nothing to check: straight home, not a guess.
    unlock = await _unlock(pin_client, headers, pin="000000")
    assert unlock.headers["Location"] == "/"
    assert auth.throttle_key_count == 0


async def test_old_locked_cookie_renews_locked(pin_client, auth, clock):
    headers = await _locked(auth)
    clock.advance(RENEW_AFTER_S + 1)
    resp = await pin_client.get("/", headers=headers)
    assert "data-lock" in await resp.text()
    claims = await _claims(auth, resp)
    assert claims.locked is True and claims.issued_at == int(clock.now)


@pytest.mark.parametrize("path", ["/messages", "/notes", "/p/u-anna"])
async def test_a_locked_page_load_renews_a_locked_cookie(
    pin_client, auth, clock, snapshots, path
):
    """What ``_may_renew`` says: a locked tablet's page load shows the
    lock screen (200) and renews — into a locked cookie, which is
    harmless; the snapshot is never read."""
    headers = await _locked(auth)
    clock.advance(RENEW_AFTER_S + 1)
    resp = await pin_client.get(path, headers=headers)
    assert "data-lock" in await resp.text()
    claims = await _claims(auth, resp)
    assert claims.locked is True
    assert snapshots.calls == []


async def test_lock_on_an_old_cookie_is_not_undone_by_renewal(pin_client, auth, clock):
    headers = await _cookie(auth)
    clock.advance(RENEW_AFTER_S + 1)
    resp = await _lock(pin_client, headers)
    assert (await _claims(auth, resp)).locked is True


async def test_unlock_on_an_old_cookie_is_not_undone_by_renewal(
    pin_client, auth, clock
):
    headers = await _locked(auth)
    clock.advance(RENEW_AFTER_S + 1)
    resp = await _unlock(pin_client, headers)
    assert (await _claims(auth, resp)).locked is False


async def test_login_signed_out_notice_only_when_asked(client):
    html = await (await client.get("/login")).text()
    assert "too many wrong PINs" not in html
    html = await (await client.get("/login?signed_out=2")).text()
    assert "too many wrong PINs" not in html


async def test_login_signed_out_notice_is_localised(client, settings_service):
    await settings_service.update(actor_is_admin=True, patch={"locale": "nl"})
    html = await (await client.get("/login?signed_out=1")).text()
    assert "Afgemeld" in html


# ─── Boards: Home, person pages, fragments, avatars ──────────────────────


def _board_style(html: str) -> list[str]:
    return re.findall(r"<style>(.*?)</style>", html, re.S)


def _assert_board_csp(resp, html: str) -> None:
    """A board page: exactly one inline <style>, admitted by its hash —
    and otherwise the strict policy (no style attributes, no inline JS)."""
    styles = _board_style(html)
    assert len(styles) == 1
    assert resp.headers["Content-Security-Policy"] == build_dashboard_csp(styles)
    assert "'unsafe-inline'" not in resp.headers["Content-Security-Policy"]
    assert re.search(r"\sstyle=", html) is None
    assert re.search(r"\son[a-z]+=", html) is None
    for tag in re.findall(r"<script\b[^>]*>", html):
        assert 'src="/static/' in tag, tag
    assert "javascript:" not in html
    # Only validated colours reach the style block.
    assert re.fullmatch(r"[.\w{}:;#\-()@ ]*", styles[0]), styles[0]


def _attr(html: str, name: str) -> str:
    match = re.search(rf'{name}="([^"]*)"', html)
    assert match, name
    return match.group(1)


async def _get(client, auth, path: str, **kw):
    return await client.get(path, headers=await _cookie(auth), **kw)


async def test_home_board(client, auth, snapshots):
    resp = await _get(client, auth, "/")
    assert resp.status == 200
    html = await resp.text()
    _assert_board_csp(resp, html)
    assert snapshots.calls == ["home"]
    # Clock, date, next up with a relative time the script can hide.
    assert "data-clock>14:32</time>" in html
    assert "Monday, October 12" in html
    assert "Swimming" in html
    assert '<span class="dash-rel" data-rel>in 28 min</span>' in html
    # Who's home (no coordinates, no addresses).
    assert "dash-presence" in html and ">Work<" in html
    # Today: lanes with blocks, the school block and today's changes.
    assert "dash-timeline" in html and "dash-block-school" in html
    # A bar too narrow for its title shows an icon; the label is spoken.
    assert '<span class="dash-visually-hidden">10:00–11:00 Dentist</span>' in html
    assert "dash-block-narrow" in html and "dash-block-icon" in html
    assert 'title="10:00' not in html  # no hover tooltips on a tablet
    assert "Cancelled" in html and "Added" in html and "Room B7" in html
    assert "At school now" in html
    # Columns: chores with glyphs + words, shopping capped, a sticky.
    assert "Chores today · 2 open" in html
    assert "⚠" in html and "2 days late" in html and "✓" in html
    assert ">Overdue:<" in html and ">Done:<" in html
    assert "Shopping · 11" in html and "+ 5 more" in html
    assert "Tea" in html and "Oats" not in html
    assert "Plumber Tue 9am" in html
    # Page wiring for dashboard.js.
    assert 'data-fragment="/fragment/home"' in html
    assert 'data-idle-minutes="0"' in html  # Home never "returns home"
    assert 'data-keep-awake="1"' in html
    assert '<noscript><meta http-equiv="refresh" content="120"></noscript>' in html


async def test_home_rail(client, auth):
    html = await (await _get(client, auth, "/")).text()
    nav = html[html.index("<nav") : html.index("</nav>")]
    assert 'aria-label="People"' in nav
    assert 'href="/" aria-current="page"' in nav
    links = re.findall(r'href="(/p/[^"]+)"', nav)
    assert links == ["/p/u-pascal", "/p/u-anna", "/p/u-mia", "/p/u-leo"]
    # A picture when there is one, initials always (the fallback).
    assert '<img src="/avatar/u-anna?v=pic1" alt=""' in nav
    assert "/avatar/u-pascal" not in html
    assert '<span class="dash-initials">P</span>' in nav
    assert "dash-rail-many" not in nav
    assert 'action="/lock"' not in html  # no PIN set


async def test_nothing_private_on_any_board(client, auth, snapshots):
    """No addresses / coordinates: the event location never renders."""
    for path in ("/", "/p/u-anna", "/p/u-mia", "/fragment/home"):
        html = await (await _get(client, auth, path)).text()
        assert "Secret Street" not in html
        assert "latitude" not in html and "52.52" not in html


async def test_only_the_snapshots_people_appear(client, auth, snapshots):
    """The service drops hidden people; the views add nobody back."""
    snap = snapshots.home_snapshot
    snapshots.home_snapshot = replace(
        snap,
        people=snap.people[:2],
        events_today=(),
        school=(),
        chores_today=(),
        next_up=None,
    )
    html = await (await _get(client, auth, "/")).text()
    assert "/p/u-mia" not in html and "/p/u-leo" not in html
    assert ">Mia<" not in html and ">Leo<" not in html
    assert "/p/u-pascal" in html and "/p/u-anna" in html


async def test_board_colours_are_classes_in_the_hashed_style(client, auth):
    html = await (await _get(client, auth, "/")).text()
    style = _board_style(html)[0]
    assert ".k0{--kc:#357bbb;--kt:" in style  # Pascal's colour
    assert "#ffee88" in style  # the sticky
    assert "body" not in style  # the bad sticky colour never lands
    assert re.search(r'class="dash-face dash-face-rail k\d+"', html)


async def test_household_theme_colour_becomes_the_accent(client, auth, household):
    household["theme"] = "#2F6F5E"
    resp = await _get(client, auth, "/")
    html = await resp.text()
    _assert_board_csp(resp, html)
    assert "--dash-accent:#2f6f5e" in _board_style(html)[0]


async def test_unreadable_theme_colour_keeps_the_built_in_accent(
    client, auth, household
):
    household["theme"] = "#FFEE88"  # 1.2:1 on white
    html = await (await _get(client, auth, "/")).text()
    assert "prefers-color-scheme:light" not in _board_style(html)[0]


async def test_a_failing_theme_read_still_renders(client, auth, household, caplog):
    household["theme"] = RuntimeError("theme db gone")
    resp = await _get(client, auth, "/")
    assert resp.status == 200
    assert "--dash-accent" not in _board_style(await resp.text())[0]
    assert "theme read failed" in caplog.text


async def test_person_page_reader(client, auth, snapshots, settings_service):
    await settings_service.update(
        actor_is_admin=True,
        patch={"idle_return_minutes": 10, "keep_screen_on": False},
    )
    resp = await _get(client, auth, "/p/u-anna")
    assert resp.status == 200
    html = await resp.text()
    _assert_board_csp(resp, html)
    assert snapshots.calls == ["person:u-anna"]
    assert (
        '<h1 class="dash-person-name">Anna <span class="dash-status">😴</span>' in html
    )
    assert "📍</span> Work" in html  # a zone name, no coordinates
    assert "Now" in html and "until 17:00" in html
    assert "Dinner with Gran" in html and "<span data-rel> · in 3 h</span>" in html
    assert "Tomorrow" in html and "Mia&#39;s birthday" in html and "All day" in html
    assert "Laundry" in html  # her chore, done today
    assert 'href="/p/u-anna" aria-current="page"' in html
    assert 'data-fragment="/fragment/p/u-anna"' in html
    assert 'data-idle-minutes="10"' in html
    assert 'data-keep-awake="0"' in html
    assert "data-picture-view" not in html


async def test_person_page_school_day(client, auth):
    html = await (await _get(client, auth, "/p/u-mia")).text()
    assert "School today" in html
    # Cancelled: struck through (class) AND labelled.
    assert re.search(r'class="dash-row k\d+ dash-st-cancelled"', html)
    assert "✕</span> Cancelled" in html
    assert "🔁</span> Changed" in html and "+</span> Added" in html
    assert "Room B12 · Keller" in html
    assert "dash-current" in html and ">now</span>" in html
    assert "First lesson" in html
    # Lessons sit in the school column only; today's list has the events.
    assert html.count("Maths</span>") == 1  # the school list (NOW card: <p>)
    assert "Dinner with Gran" in html


async def test_tomorrow_lists_events_and_the_first_lesson_in_time_order(
    client, auth, snapshots
):
    page = snapshots.pages["u-mia"]
    gym = event("g", "Gym", at(7, day=TOMORROW), at(7, 20, day=TOMORROW), "u-mia")
    late = event("l", "Party", at(19, day=TOMORROW), at(21, day=TOMORROW), "u-mia")
    snapshots.pages["u-mia"] = replace(page, tomorrow_items=(late, gym))
    html = await (await _get(client, auth, "/p/u-mia")).text()
    tomorrow = html[html.index("dash-agenda-tomorrow") :]
    assert (
        tomorrow.index("Gym") < tomorrow.index("First lesson") < tomorrow.index("Party")
    )


# ─── Picture view ────────────────────────────────────────────────────────


def _visible_text(html: str) -> str:
    """What a sighted viewer can read: no <head>, style or script, no
    visually-hidden or ``hidden`` text, no tags (so no attributes —
    aria-label and data-say text is for screen readers and speech)."""
    html = re.sub(r"<head>.*?</head>", "", html, flags=re.S)
    html = re.sub(r"<span\b[^>]*\shidden>[^<]*</span>", "", html)
    html = re.sub(r"<(style|script)\b.*?</\1>", "", html, flags=re.S)
    html = re.sub(
        r'<span class="[^"]*\bdash-visually-hidden\b[^"]*">[^<]*</span>', "", html
    )
    return unescape(re.sub(r"<[^>]+>", " ", html))


_CAPTION = re.compile(
    r'<span class="(dash-pv-(?:cap|sec)\b[^"]*)" aria-hidden="true">([^<]*)</span>'
)


def _captions(html: str, kind: str = "cap") -> list[str]:
    """The helper captions (``cap``) or section labels (``sec``) in
    ``html``, unescaped, in page order."""
    return [
        unescape(text)
        for cls, text in _CAPTION.findall(html)
        if cls.split()[0] == f"dash-pv-{kind}"
    ]


def _pv_section(html: str, cls: str) -> str:
    start = html.index(f'class="{cls}"')
    return html[start : html.index("</section>", start)]


def _assert_only_names_and_icons(html: str, people) -> None:
    """Outside the helper captions and section labels (small print for a
    grown-up reading along), the picture view shows names and pictures
    only — no digits, no other words."""
    assert "dash-pv-cap" not in _CAPTION.sub("", html), "a caption not aria-hidden"
    text = _visible_text(_CAPTION.sub("", html))
    assert re.search(r"\d", text) is None, re.findall(r"\S*\d\S*", text)
    allowed = {w for p in people for w in (*p.display_name.split(), p.initials)}
    words = set(re.findall(r"[^\W\d_]+", text))
    assert words <= allowed, words - allowed


async def test_picture_page(client, auth, snapshots):
    snapshots.pages["u-leo"] = leo_page()
    resp = await _get(client, auth, "/p/u-leo")
    assert resp.status == 200
    html = await resp.text()
    _assert_board_csp(resp, html)
    assert "data-picture-view" in html and "dash-person-name" not in html
    _assert_only_names_and_icons(html, PEOPLE)
    # Small helper captions for a grown-up reading along: titles under the
    # tiles, NOW / NEXT with their time, the chore names …
    assert _captions(html) == [
        "Kunst · 10:00",
        "Swimming · 15:00",
        "Lesen",
        "Pause",
        "Turnen",
        "Kunst",
        "Swimming",
        "Dinner with Gran",
        "School",
        "Katze füttern",
        "Zähneputzen",
    ]
    # … the cancelled lesson's caption struck through …
    assert (
        '<span class="dash-pv-cap dash-pv-cap-cancelled" aria-hidden="true">'
        "Turnen</span>"
    ) in html
    # … and quiet section labels.
    assert _captions(html, "sec") == [
        "Now",
        "Next",
        "My day",
        "Tomorrow",
        "My jobs",
        "Who's home",
    ]
    # … but every tile speaks its real title.
    for label in (
        'aria-label="It&#39;s 10:30"',
        'aria-label="Now: Kunst"',
        'aria-label="Next: Swimming, in 4 h"',
        'aria-label="08:00–08:45 Lesen"',
        'aria-label="09:00–09:45 Turnen, cancelled"',
        'aria-label="15:00–16:00 Swimming"',
        'aria-label="To do: Katze füttern"',
        'aria-label="Done: Zähneputzen"',
        'aria-label="08:00 School"',
        'aria-label="Sleeps until then: 1"',
        'aria-label="Morning"',
    ):
        assert label in html, label
    # The analog clock: hands from the household time (10:30).
    assert 'transform="rotate(315 100 100)" data-hand="hour"' in html
    assert 'transform="rotate(180 100 100)" data-hand="minute"' in html
    # Swimming is hours away: the wedge is a full disc, the pie too.
    assert re.search(r'<g class="dash-pv-wedge k\d+" data-rel><circle', html)
    assert 'class="dash-pv-pie" viewBox="0 0 100 100"' in html
    # MY DAY: icons, the cancelled lesson with ✕, the running one framed.
    assert ">📖</span>" in html and ">🧸</span>" in html and ">🏊</span>" in html
    assert re.search(r'dash-pv-tile k\d+ dash-pv-past dash-pv-cancelled"', html)
    assert 'aria-hidden="true">✕</span>' in html
    assert re.search(r'dash-pv-tile k\d+ dash-pv-current" data-live>', html)
    # Jobs (☐ / ✓) and who's home (faces, not the page owner).
    assert ">🐱</span>" in html and ">🦷</span>" in html
    assert '<span class="dash-pv-check" aria-hidden="true">✓</span>' in html
    where = html[html.index("dash-pv-where") :]
    assert "Pascal" in where and "Anna" in where and "Leo" not in where
    # The rail's "Home" is spoken only; the 88 px face is there.
    assert '<span class="dash-rail-name dash-visually-hidden">Home</span>' in html
    assert "dash-face dash-face-pv" in html
    assert "🎈" not in html


async def test_picture_page_without_now_shows_free_time(client, auth, snapshots):
    snapshots.pages["u-leo"] = leo_page(now=at(12), now_item=None)
    html = await (await _get(client, auth, "/p/u-leo")).text()
    assert 'aria-label="Free time">🧸</span>' in html  # Leo is away
    assert _captions(html)[:2] == ["Free time", "Swimming · 15:00"]
    assert "dash-pv-arrow" in html
    _assert_only_names_and_icons(html, PEOPLE)


async def test_picture_page_empty_day_is_a_balloon(client, auth, snapshots):
    snapshots.pages["u-leo"] = leo_page(
        now_item=None,
        next_item=None,
        today_items=(),
        tomorrow_items=(),
        school=None,
        school_tomorrow_first=None,
        chores=(),
        people=(LEO,),
    )
    html = await (await _get(client, auth, "/p/u-leo")).text()
    assert 'aria-label="Nothing planned today">🎈</span>' in html
    assert "dash-pv-nownext" not in html and "dash-pv-parts" not in html
    assert "dash-pv-more" not in html and "dash-pv-wedge" not in html
    _assert_only_names_and_icons(html, (LEO,))


async def test_picture_page_offline_banner_is_wordless(client, auth, snapshots):
    snapshots.pages["u-leo"] = leo_page()
    html = await (await _get(client, auth, "/p/u-leo")).text()
    assert (
        '<span class="dash-offline-text dash-visually-hidden">'
        "Can&#39;t reach Social Home — showing info from 10:30</span>"
    ) in html


async def test_picture_page_never_shows_hidden_people(client, auth, snapshots):
    # The snapshot service leaves hidden people out; nothing re-adds them.
    snapshots.pages["u-leo"] = leo_page(people=(PASCAL, LEO))
    html = await (await _get(client, auth, "/p/u-leo")).text()
    for name in ("Anna", "Mia", "u-anna", "u-mia"):
        assert name not in html, name
    assert (await _get(client, auth, "/p/u-hidden")).status == 404


async def test_picture_page_is_locked_like_any_other(pin_client, auth, snapshots):
    snapshots.pages["u-leo"] = leo_page()
    page = await pin_client.get("/p/u-leo", headers=await _locked(auth))
    frag = await pin_client.get("/fragment/p/u-leo", headers=await _locked(auth))
    assert frag.headers["X-Dashboard-Locked"] == "1"
    for html in (await page.text(), await frag.text()):
        assert "Leo" not in html and "data-picture-view" not in html
        assert "Kunst" not in html and "🎨" not in html
        assert "Swimming" not in html and "Katze" not in html
        assert "dash-pv-cap" not in html and "dash-pv-sec" not in html
    assert snapshots.calls == []


async def test_picture_fragment_matches_the_page(client, auth, snapshots):
    snapshots.pages["u-leo"] = leo_page()
    page = await (await _get(client, auth, "/p/u-leo")).text()
    frag = await (await _get(client, auth, "/fragment/p/u-leo")).text()
    assert "data-picture-view" in frag and "<nav" not in frag
    assert _attr(frag, "data-view-key") == _attr(page, "data-view-key")


async def test_turning_the_picture_view_on_changes_the_view_key(
    client, auth, snapshots
):
    snapshots.pages["u-leo"] = leo_page()
    picture = _attr(
        await (await _get(client, auth, "/p/u-leo")).text(), "data-view-key"
    )
    snapshots.pages["u-leo"] = leo_page(person=replace(LEO, picture_view=False))
    reader = await (await _get(client, auth, "/fragment/p/u-leo")).text()
    assert "dash-person-name" in reader
    assert _attr(reader, "data-view-key") != picture


async def test_picture_night_mode(client, auth, snapshots):
    snapshots.pages["u-leo"] = leo_page(
        now=at(22, 47), is_night=True, now_item=None, next_item=None
    )
    resp = await _get(client, auth, "/p/u-leo")
    html = await resp.text()
    _assert_board_csp(resp, html)
    night = html[
        html.index('<section class="dash-night-view') : html.index(
            '<div class="dash-day"'
        )
    ]
    assert "dash-pv-clock-night" in night
    assert 'transform="rotate(323.5 100 100)" data-hand="hour"' in night
    assert "dash-pv-wedge" not in night
    assert '<span aria-hidden="true">🌙</span>' in night
    assert 'aria-label="Tomorrow 08:00: School">🎒</span>' in night
    # One small caption for a grown-up; the icon already speaks it.
    assert _captions(night) == ["Tomorrow · School 08:00"]
    _assert_only_names_and_icons(night, PEOPLE)
    assert "dash-night-date" not in night
    # The day view underneath is the picture view too.
    assert "data-picture-view" in html[html.index("data-day-view") :]


async def test_picture_night_mode_small_hours(client, auth, snapshots):
    snapshots.pages["u-leo"] = leo_page(
        now=at(5), is_night=True, now_item=None, next_item=SWIM
    )
    html = await (await _get(client, auth, "/p/u-leo")).text()
    night = html[
        html.index('<section class="dash-night-view') : html.index(
            '<div class="dash-day"'
        )
    ]
    assert '<span aria-hidden="true">🌅</span>' in night
    assert 'aria-label="Today 15:00: Swimming">🏊</span>' in night
    assert _captions(night) == ["Today · Swimming 15:00"]


async def test_picture_night_mode_with_nothing_ahead(client, auth, snapshots):
    snapshots.pages["u-leo"] = leo_page(
        now=at(23),
        is_night=True,
        now_item=None,
        next_item=None,
        tomorrow_items=(),
        school_tomorrow_first=None,
    )
    html = await (await _get(client, auth, "/p/u-leo")).text()
    assert "dash-pv-night-next" not in html
    assert "dash-pv-clock-night" in html


@pytest.mark.parametrize("path", ["/p/u-nobody", "/p/u-hidden"])
async def test_unknown_or_hidden_person_is_404(client, auth, path):
    resp = await _get(client, auth, path)
    assert resp.status == 404
    html = await resp.text()
    assert "This page isn&#39;t on the dashboard." in html
    assert 'href="/"' in html
    assert "<nav" not in html and "u-anna" not in html
    assert resp.headers["Cache-Control"] == "no-store"


async def test_home_fragment_is_main_only(client, auth):
    page = await (await _get(client, auth, "/")).text()
    resp = await _get(client, auth, "/fragment/home")
    assert resp.status == 200
    frag = await resp.text()
    assert frag.lstrip().startswith('<div class="dash-view" data-view-key=')
    assert "<html" not in frag and "<style" not in frag
    assert "dash-rail" not in frag  # the rail is outside <main>
    assert "Swimming" in frag and "data-offline" in frag
    assert _attr(frag, "data-view-key") == _attr(page, "data-view-key")
    assert resp.headers["Cache-Control"] == "no-store"


async def test_person_fragment(client, auth):
    page = await (await _get(client, auth, "/p/u-anna")).text()
    frag = await (await _get(client, auth, "/fragment/p/u-anna")).text()
    assert "<nav" not in frag and "Dinner with Gran" in frag
    assert _attr(frag, "data-view-key") == _attr(page, "data-view-key")
    # The view key differs between pages (a swap never crosses pages).
    home = await (await _get(client, auth, "/fragment/home")).text()
    assert _attr(home, "data-view-key") != _attr(frag, "data-view-key")


async def test_person_fragment_for_a_hidden_person_is_404(client, auth):
    resp = await _get(client, auth, "/fragment/p/u-hidden")
    assert resp.status == 404
    assert "<" not in await resp.text()


async def test_view_key_changes_with_the_colours(client, auth, snapshots):
    frag = await (await _get(client, auth, "/fragment/home")).text()
    before = _attr(frag, "data-view-key")
    snap = snapshots.home_snapshot
    snapshots.home_snapshot = replace(
        snap, people=(replace(snap.people[0], color="#000000"), *snap.people[1:])
    )
    frag = await (await _get(client, auth, "/fragment/home")).text()
    assert _attr(frag, "data-view-key") != before


async def test_fragment_without_a_session_is_401(client):
    for path in ("/fragment/home", "/fragment/p/u-anna"):
        resp = await client.get(path, allow_redirects=False)
        assert resp.status == 401


async def test_offline_banner_is_rendered_hidden_with_the_snapshot_time(client, auth):
    html = await (await _get(client, auth, "/")).text()
    assert '<div class="dash-offline" data-offline role="status" hidden>' in html
    assert "Can&#39;t reach Social Home — showing info from 14:32" in html
    assert 'data-reload aria-label="Try again"' in html


# Lock wins over every board route — and the snapshot is never read.


@pytest.mark.parametrize(
    "path", ["/", "/p/u-anna", "/p/u-nobody", "/fragment/home", "/fragment/p/u-anna"]
)
async def test_locked_tablet_never_reads_a_snapshot(pin_client, auth, snapshots, path):
    resp = await pin_client.get(path, headers=await _locked(auth))
    assert resp.status == 200
    html = await resp.text()
    assert "Anna" not in html and "Swimming" not in html
    if path.startswith("/fragment/"):
        assert resp.headers["X-Dashboard-Locked"] == "1"
    else:
        assert "data-lock" in html
    assert snapshots.calls == []


async def test_locked_real_avatar_is_423(pin_client, auth, snapshots):
    snapshots.pictures["u-anna"] = (b"RIFF\0\0\0\0WEBPVP8 ", "pic1")
    resp = await pin_client.get("/avatar/u-anna", headers=await _locked(auth))
    assert resp.status == 423
    assert snapshots.calls == []


async def test_lock_button_in_the_rail_with_a_pin(pin_client, auth):
    html = await (await _get(pin_client, auth, "/")).text()
    rail = html[html.index("<nav") : html.index("</nav>")]
    assert 'class="dash-rail-lock"' in rail and 'action="/lock"' in rail
    assert 'aria-label="Lock screen"' in rail


# States: one person, many people, empty, sections off.


async def test_one_person_has_no_rail_but_keeps_the_lock(pin_client, auth, snapshots):
    snap = snapshots.home_snapshot
    snapshots.home_snapshot = replace(snap, people=(snap.people[0],))
    html = await (await _get(pin_client, auth, "/")).text()
    assert '<nav class="dash-rail' not in html
    assert 'class="dash-corner"' in html and 'action="/lock"' in html
    assert "dash-presence" not in html  # no "who's home" for one person


async def test_one_person_without_a_pin_has_no_corner(client, auth, snapshots):
    snap = snapshots.home_snapshot
    snapshots.home_snapshot = replace(snap, people=(snap.people[0],))
    html = await (await _get(client, auth, "/")).text()
    assert "dash-corner" not in html and '<nav class="dash-rail' not in html


async def test_more_than_six_people_scroll_and_list(client, auth, snapshots):
    many = tuple(
        replace(PASCAL, user_id=f"u-{i}", display_name=f"Person {i}") for i in range(7)
    )
    ev = event("x", "Picnic", at(16), at(18), "u-0", "u-3")
    snapshots.home_snapshot = replace(
        snapshots.home_snapshot,
        people=many,
        events_today=(ev,),
        school=(replace(MIA_SCHOOL, person_id="u-2"),),
        next_up=None,
    )
    html = await (await _get(client, auth, "/")).text()
    assert "dash-rail dash-rail-many" in html
    assert "dash-timeline" not in html
    assert '<ol class="dash-agenda">' in html
    assert "Picnic" in html and "16:00–18:00" in html
    assert "08:20–15:50" in html and "School" in html


async def test_empty_day_and_empty_columns(client, auth, snapshots):
    snapshots.home_snapshot = replace(
        snapshots.home_snapshot,
        events_today=(),
        school=(),
        next_up=None,
        tomorrow_first=None,
        chores_today=(),
        shopping=(),
        shopping_total=0,
        stickies=(),
    )
    html = await (await _get(client, auth, "/")).text()
    assert "Nothing planned today" in html and "🎈" in html
    assert "dash-columns" not in html
    assert "dash-next" not in html
    assert "At school now" not in html


async def test_many_people_with_an_empty_day(client, auth, snapshots):
    many = tuple(replace(PASCAL, user_id=f"u-{i}") for i in range(8))
    snapshots.home_snapshot = replace(
        snapshots.home_snapshot, people=many, events_today=(), school=()
    )
    html = await (await _get(client, auth, "/")).text()
    assert "Nothing planned today" in html


async def test_sections_off_leave_their_parts_out(client, auth, snapshots):
    snapshots.home_snapshot = replace(
        snapshots.home_snapshot, sections=frozenset({"shopping"})
    )
    html = await (await _get(client, auth, "/")).text()
    assert "dash-today" not in html
    assert "Chores today" not in html and "Notes" not in html
    assert "dash-presence" not in html
    assert "Shopping · 11" in html


async def test_person_page_sections_off_and_empty(client, auth, snapshots):
    snapshots.pages["u-anna"] = person(
        sections=frozenset({"calendar"}),
        now_item=None,
        next_item=None,
        today_items=(),
        tomorrow_items=(),
    )
    html = await (await _get(client, auth, "/p/u-anna")).text()
    assert "Nothing planned today" in html
    assert "dash-nownext" not in html
    assert "Chores" not in html
    assert "dash-person-where" not in html
    assert "Tomorrow" not in html


# Night mode.


async def test_night_mode_home(client, auth, snapshots):
    day = _attr(await (await _get(client, auth, "/")).text(), "data-view-key")
    snapshots.home_snapshot = replace(
        snapshots.home_snapshot, is_night=True, now=at(22, 47)
    )
    resp = await _get(client, auth, "/")
    html = await resp.text()
    _assert_board_csp(resp, html)
    assert '<body class="dash-board dash-night"' in html
    night = html[html.index("dash-night-view") : html.index("data-day-view")]
    assert "data-clock>22:47</time>" in night
    assert "Tomorrow" in night and "07:30" in night and "School" in night
    assert "Swimming" not in night and "Dentist" not in night
    # Shown by CSS once dashboard.js runs (html.dash-js) — survives swaps.
    assert '<p class="dash-night-hint" data-wake-hint>' in night
    # The day view is there for a tap, hidden by CSS.
    assert '<div class="dash-day" data-day-view>' in html
    assert _attr(html, "data-view-key") != day


async def test_night_mode_without_a_morning_item(client, auth, snapshots):
    snapshots.home_snapshot = replace(
        snapshots.home_snapshot, is_night=True, now=at(22, 47), tomorrow_first=None
    )
    html = await (await _get(client, auth, "/")).text()
    assert "dash-night-next" not in html


async def test_night_mode_person_with_an_event(client, auth, snapshots):
    gym = event("g", "Gym", at(7, day=TOMORROW), at(8, day=TOMORROW), "u-anna")
    snapshots.pages["u-anna"] = person(is_night=True, now=at(23), tomorrow_items=(gym,))
    html = await (await _get(client, auth, "/p/u-anna")).text()
    night = html[html.index("dash-night-view") : html.index("data-day-view")]
    assert "Tomorrow" in night and "07:00" in night and "Gym" in night
    assert "dash-faces" not in night  # her own page: no face needed


# Localised.


async def test_board_in_german(client, auth, settings_service):
    await settings_service.update(actor_is_admin=True, patch={"locale": "de"})
    html = await (await _get(client, auth, "/")).text()
    assert '<html lang="de"' in html
    assert "Montag, 12. Oktober" in html
    assert "in 28 Min." in html
    assert "Aufgaben heute · 2 offen" in html
    assert "2 Tage überfällig" in html
    assert "Fällt aus" in html
    page = await (await _get(client, auth, "/p/u-anna")).text()
    assert "bis 17:00" in page and "Ganztägig" in page


# Avatars.


async def test_avatar_of_a_shown_person(client, auth, snapshots):
    body = b"RIFF\0\0\0\0WEBPVP8 data"
    snapshots.pictures["u-anna"] = (body, "pic1")
    resp = await _get(client, auth, "/avatar/u-anna")
    assert resp.status == 200
    assert await resp.read() == body
    assert resp.content_type == "image/webp"
    assert resp.headers["ETag"] == '"pic1"'
    assert resp.headers["Cache-Control"] == "private, no-cache"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    again = await client.get(
        "/avatar/u-anna",
        headers={**await _cookie(auth), "If-None-Match": '"pic1"'},
    )
    assert again.status == 304
    assert again.headers["Cache-Control"] == "private, no-cache"


async def test_avatar_hidden_unknown_or_without_picture_is_404(client, auth, snapshots):
    for uid in ("u-hidden", "u-nobody", "u-pascal"):
        resp = await _get(client, auth, f"/avatar/{uid}")
        assert resp.status == 404
        assert resp.headers["Cache-Control"] == "no-store"
    assert snapshots.calls == [
        "avatar:u-hidden",
        "avatar:u-nobody",
        "avatar:u-pascal",
    ]


async def test_avatar_etag_never_echoes_an_odd_hash(client, auth, snapshots):
    body = b"\x89PNG\r\n\x1a\n...."
    snapshots.pictures["u-anna"] = (body, 'x"\r\nSet-Cookie: a=b')
    resp = await _get(client, auth, "/avatar/u-anna")
    assert resp.status == 200
    assert re.fullmatch(r'"[0-9a-f]{32}"', resp.headers["ETag"])
    assert resp.content_type == "image/png"


async def test_avatar_without_a_session_redirects_to_login(client):
    resp = await client.get("/avatar/u-anna", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"].startswith("/login?next=")


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (b"RIFF\0\0\0\0WEBPVP8 ", "image/webp"),
        (b"\x89PNG\r\n\x1a\nrest", "image/png"),
        (b"\xff\xd8\xff\xe0rest", "image/jpeg"),
        (b"GIF89arest", "image/gif"),
        (b"GIF87arest", "image/gif"),
        (b"<svg onload=alert(1)>", "application/octet-stream"),
        (b"", "application/octet-stream"),
    ],
)
def test_image_type(body, kind):
    assert image_type(body) == kind


# UX round.


async def test_next_band_falls_back_to_tomorrow_when_today_is_done(
    client, auth, snapshots
):
    gym = event("g", "Gym", at(7, day=TOMORROW), at(8, day=TOMORROW), "u-pascal")
    snapshots.home_snapshot = replace(
        snapshots.home_snapshot, now=at(21), next_up=None, tomorrow_first=gym
    )
    html = await (await _get(client, auth, "/")).text()
    start = html.index("dash-next")
    band = html[start : html.index("</section>", start)]
    assert "dash-next-tomorrow" in band
    assert ">Tomorrow</h2>" in band
    assert '<span class="dash-next-time">07:00</span>' in band
    assert "Gym" in band
    assert "dash-face" in band and "Pascal" in band
    assert "data-rel" not in band  # no "in 10 h"


async def test_next_band_tomorrow_lesson_says_school(client, auth, snapshots):
    snapshots.home_snapshot = replace(snapshots.home_snapshot, next_up=None)
    html = await (await _get(client, auth, "/")).text()
    start = html.index("dash-next")
    band = html[start : html.index("</section>", start)]
    assert "🎒</span> School" in band and "07:30" in band


async def test_home_has_the_phone_list_next_to_the_lanes(client, auth):
    html = await (await _get(client, auth, "/")).text()
    assert "dash-timeline" in html
    narrow = html[html.index('class="dash-agenda dash-agenda-narrow"') :]
    narrow = narrow[: narrow.index("</ol>")]
    # In time order, school as one row, with whose it is.
    order = ["Mia&#39;s birthday", "08:20–15:50", "Work", "Dentist", "Swimming"]
    positions = [narrow.index(text) for text in order]
    assert positions == sorted(positions)
    assert "dash-row-school" in narrow and "dash-faces" in narrow


async def test_no_phone_list_without_lanes(client, auth, snapshots):
    snapshots.home_snapshot = replace(
        snapshots.home_snapshot, events_today=(), school=()
    )
    html = await (await _get(client, auth, "/")).text()
    assert "dash-agenda-narrow" not in html


async def test_person_page_paints_events_in_the_persons_colour(client, auth, snapshots):
    """A shared event keeps whichever member's calendar colour won the
    merge — on Anna's page it is painted in Anna's colour instead."""
    shared = replace(DINNER, color=PASCAL.color)
    snapshots.pages["u-anna"] = person(next_item=shared, today_items=(WORK, shared))
    html = await (await _get(client, auth, "/p/u-anna")).text()
    style = _board_style(html)[0]
    anna_cls = re.search(r"\.(k\d+)\{--kc:#c44d72", style).group(1)
    pascal = re.search(r"\.(k\d+)\{--kc:#357bbb", style).group(1)
    rows = re.findall(r'<li class="dash-row (k\d+)', html)
    assert rows and set(rows) == {anna_cls}
    assert f'class="dash-nn {anna_cls}"' in html
    assert f'class="dash-row {pascal}' not in html


# Review round: security.


@pytest.mark.parametrize(
    "path",
    [
        "/avatar/u-anna",
        "/static/dashboard.css",
        "/fragment/home",
        "/fragment/home/messages",
        "/fragment/home/notes",
        "/fragment/p/u-anna",
        "/p/u-anna/extra",
        "/messages/extra",
        "/notes/",
        "/nowhere",
    ],
)
async def test_only_page_routes_renew_the_cookie(
    pin_client, auth, clock, snapshots, path
):
    """S1: an avatar / static / fragment response never sets a cookie — a
    late one, sent with the pre-lock cookie, could overwrite the lock."""
    snapshots.pictures["u-anna"] = (b"RIFF\0\0\0\0WEBPVP8 ", "pic1")
    snapshots.home_snapshot = replace(snapshots.home_snapshot, messages_on=True)
    headers = await _cookie(auth)
    clock.advance(RENEW_AFTER_S + 60)
    resp = await pin_client.get(path, headers=headers, allow_redirects=False)
    assert COOKIE_NAME not in resp.cookies, (path, resp.status)


@pytest.mark.parametrize("path", ["/", "/p/u-anna", "/messages", "/notes"])
async def test_page_routes_still_renew(pin_client, auth, clock, snapshots, path):
    snapshots.home_snapshot = replace(snapshots.home_snapshot, messages_on=True)
    headers = await _cookie(auth)
    clock.advance(RENEW_AFTER_S + 60)
    resp = await pin_client.get(path, headers=headers, allow_redirects=False)
    assert resp.status == 200
    assert COOKIE_NAME in resp.cookies


@pytest.mark.parametrize("path", ["/", "/p/u-anna", "/messages", "/notes"])
async def test_head_on_a_page_is_the_get_without_a_body(
    pin_client, auth, clock, snapshots, path
):
    """Review: HEAD was 405 on the board routes while documented."""
    snapshots.home_snapshot = replace(snapshots.home_snapshot, messages_on=True)
    headers = await _cookie(auth)
    clock.advance(RENEW_AFTER_S + 60)
    resp = await pin_client.head(path, headers=headers, allow_redirects=False)
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/html")
    assert "Content-Security-Policy" in resp.headers
    assert await resp.read() == b""
    assert COOKIE_NAME in resp.cookies  # renews like the GET


async def test_head_follows_the_get_status(client, auth, snapshots):
    snapshots.home_snapshot = replace(
        snapshots.home_snapshot, messages_on=True, is_night=True, now=at(22, 47)
    )
    resp = await client.head(
        "/messages", headers=await _cookie(auth), allow_redirects=False
    )
    assert resp.status == 303 and resp.headers["Location"] == "/"
    resp = await client.head(
        "/p/u-nobody", headers=await _cookie(auth), allow_redirects=False
    )
    assert resp.status == 404


@pytest.mark.parametrize("path", ["/messages", "/notes"])
async def test_tab_that_did_not_render_never_renews(
    pin_client, auth, clock, snapshots, path
):
    """A tab that is off (404) or redirected at night (303) renews nothing."""
    snap = snapshots.home_snapshot
    headers = await _cookie(auth)
    clock.advance(RENEW_AFTER_S + 60)
    snapshots.home_snapshot = replace(
        snap, messages_on=False, sections=snap.sections - {"stickies"}
    )
    resp = await pin_client.get(path, headers=headers, allow_redirects=False)
    assert resp.status == 404 and COOKIE_NAME not in resp.cookies
    snapshots.home_snapshot = replace(
        snap, messages_on=True, is_night=True, now=at(22, 47)
    )
    resp = await pin_client.get(path, headers=headers, allow_redirects=False)
    assert resp.status == 303 and COOKIE_NAME not in resp.cookies


async def test_late_avatar_response_cannot_lift_the_lock(
    pin_client, auth, clock, snapshots
):
    """S1 repro: avatars fetched with an old cookie, a /lock in between, a
    late avatar response — the tablet stays locked."""
    snapshots.pictures["u-anna"] = (b"RIFF\0\0\0\0WEBPVP8 ", "pic1")
    snapshots.pictures["u-mia"] = (b"RIFF\0\0\0\0WEBPVP8 ", "pic2")
    before = await _cookie(auth)
    clock.advance(RENEW_AFTER_S + 60)
    first = await pin_client.get("/avatar/u-anna", headers=before)
    assert COOKIE_NAME not in first.cookies
    lock = await _lock(pin_client, before)
    late = await pin_client.get("/avatar/u-mia", headers=before)
    assert COOKIE_NAME not in late.cookies
    page = await pin_client.get("/", headers=_jar(lock), allow_redirects=False)
    assert "data-lock" in await page.text()
    again = await pin_client.get("/", headers=before, allow_redirects=False)
    assert "data-lock" in await again.text()


# Review round: quality.


async def _home_key(client, auth) -> str:
    return _attr(
        await (await _get(client, auth, "/fragment/home")).text(), "data-view-key"
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"locale": "de"},
        {"idle_return_minutes": 10},
        {"keep_screen_on": False},
    ],
)
async def test_view_key_follows_page_settings(client, auth, settings_service, patch):
    """Q7: what lives outside <main> (lang, data-*) reloads the page."""
    before = await _home_key(client, auth)
    await settings_service.update(actor_is_admin=True, patch=patch)
    assert await _home_key(client, auth) != before


async def test_view_key_follows_the_pin(client, auth, settings_service):
    before = await _home_key(client, auth)
    await settings_service.set_pin(actor_is_admin=True, pin=PIN)
    assert await _home_key(client, auth) != before


@pytest.mark.parametrize(
    "change", [{"display_name": "Pascal B."}, {"picture_hash": "new"}]
)
async def test_view_key_follows_the_rail_faces(client, auth, snapshots, change):
    before = await _home_key(client, auth)
    snap = snapshots.home_snapshot
    snapshots.home_snapshot = replace(
        snap, people=(replace(snap.people[0], **change), *snap.people[1:])
    )
    assert await _home_key(client, auth) != before


async def test_overflowing_bars_show_plus_n(client, auth, snapshots):
    snapshots.home_snapshot = replace(
        snapshots.home_snapshot,
        events_today=tuple(
            event(f"o{n}", f"Thing {n}", at(10), at(14), "u-anna") for n in range(5)
        ),
        school=(),
    )
    html = await (await _get(client, auth, "/")).text()
    assert '<span class="dash-block-more" aria-hidden="true">+2</span>' in html
    assert "10:00–14:00 Thing 2; 10:00–14:00 Thing 3; 10:00–14:00 Thing 4" in html


async def test_school_day_without_events_has_no_empty_today_card(
    client, auth, snapshots
):
    """Q13: lessons only, nothing tomorrow → no empty "Today" card."""
    page = snapshots.pages["u-mia"]
    snapshots.pages["u-mia"] = replace(
        page,
        today_items=tuple(i for i in page.today_items if i.item_type == "lesson"),
        tomorrow_items=(),
        school_tomorrow_first=None,
    )
    html = await (await _get(client, auth, "/p/u-mia")).text()
    assert "School today" in html
    assert 'id="dash-day-label"' not in html
    assert "Nothing planned today" not in html


# ─── Household-chat messages ─────────────────────────────────────────────

_SAY_ANNA = (
    'data-say="Anna says: Dinner at 7 &lt;b&gt;&#34;sharp&#34;&lt;/b&gt; @Pascal"'
)


async def test_home_messages_card_under_next(client, auth, snapshots):
    snapshots.home_snapshot = replace(snapshots.home_snapshot, messages=MESSAGES)
    resp = await _get(client, auth, "/")
    html = await resp.text()
    _assert_board_csp(resp, html)
    # In the header's side column, right under NEXT; who's home sits in
    # the clock column, under the date.
    head = html[html.index('<header class="dash-top">') : html.index("</header>")]
    assert head.index('class="dash-next ') < head.index('class="dash-msgs"')
    assert head.index('class="dash-presence"') < head.index('class="dash-top-side"')
    assert '<h2 class="dash-label" id="dash-msgs-label">Messages</h2>' in head
    # Real buttons; the sentence to speak is an escaped attribute — the
    # message's markup is text, never HTML.
    assert head.count('<button type="button" class="dash-msg') == 3
    assert _SAY_ANNA in head and "<b>" not in html
    assert 'data-say="Pascal sent a photo."' in head
    assert 'data-say="Mia says: Can I go to Lena&#39;s?"' in head
    assert '<span id="dash-say-hint" hidden>Read aloud</span>' in head
    assert "aria-describedby" not in head  # added by the script with a voice
    # The new one: accent, "new" chip, relative time (hidden when stale).
    row = head[head.index('data-msg="m1"') : head.index('data-msg="m2"')]
    assert 'class="dash-msg dash-msg-new"' in head
    assert '<span class="dash-tag dash-tag-now">new</span>' in row
    assert "data-rel>4 min ago</time>" in row
    assert '<span class="dash-visually-hidden">Message from Anna:</span>' in row
    # Older ones: clock time, an icon + word for media, the transcript.
    row = head[head.index('data-msg="m2"') : head.index('data-msg="m3"')]
    assert "dash-msg-new" not in row and ">new<" not in row
    assert ">13:52</time>" in row
    assert '<span aria-hidden="true">📷</span> Photo' in row
    assert '<span aria-hidden="true">🎤</span> Can I go to Lena&#39;s?' in head
    # The face is the 40 px one, named once (by the "Message from" text).
    assert "dash-face dash-face-msg" in row
    assert '<span class="dash-visually-hidden">Pascal</span>' not in row


async def test_home_without_messages_has_no_card(client, auth):
    html = await (await _get(client, auth, "/")).text()
    assert "dash-msgs" not in html and "data-say=" not in html
    assert "dash-say-hint" not in html


async def test_home_messages_without_a_next_band(client, auth, snapshots):
    snapshots.home_snapshot = replace(
        snapshots.home_snapshot, next_up=None, tomorrow_first=None, messages=MESSAGES
    )
    html = await (await _get(client, auth, "/")).text()
    assert "dash-next" not in html
    assert '<div class="dash-top-side">' in html and 'class="dash-msgs"' in html


async def test_home_without_next_or_messages_has_no_side_column(
    client, auth, snapshots
):
    snapshots.home_snapshot = replace(
        snapshots.home_snapshot, next_up=None, tomorrow_first=None
    )
    html = await (await _get(client, auth, "/")).text()
    assert "dash-top-side" not in html


async def test_messages_of_people_not_shown_are_left_out(client, auth, snapshots):
    snapshots.home_snapshot = replace(
        snapshots.home_snapshot,
        messages=(message("x", "u-hidden", 2, text="Secret plan"), *MESSAGES),
    )
    html = await (await _get(client, auth, "/")).text()
    assert "Secret plan" not in html and 'data-msg="x"' not in html
    assert 'data-msg="m1"' in html


async def test_messages_swap_in_without_a_reload(client, auth, snapshots):
    page = await (await _get(client, auth, "/")).text()
    snapshots.home_snapshot = replace(snapshots.home_snapshot, messages=MESSAGES)
    frag = await (await _get(client, auth, "/fragment/home")).text()
    assert _SAY_ANNA in frag
    # Same view key: a new (or deleted) message just swaps in.
    assert _attr(frag, "data-view-key") == _attr(page, "data-view-key")


async def test_no_messages_at_night(client, auth, snapshots):
    snapshots.home_snapshot = replace(
        snapshots.home_snapshot, is_night=True, now=at(22, 47), messages=MESSAGES
    )
    html = await (await _get(client, auth, "/")).text()
    assert "dash-night-view" in html
    assert "data-say=" not in html and "Dinner at 7" not in html
    snapshots.pages["u-leo"] = leo_page(is_night=True, messages=MESSAGES)
    html = await (await _get(client, auth, "/p/u-leo")).text()
    assert "data-say=" not in html and "dash-pv-msg" not in html


@pytest.mark.parametrize("path", ["/", "/fragment/home", "/p/u-leo"])
async def test_a_locked_tablet_shows_no_message(pin_client, auth, snapshots, path):
    snapshots.home_snapshot = replace(snapshots.home_snapshot, messages=MESSAGES)
    snapshots.pages["u-leo"] = leo_page(messages=MESSAGES)
    html = await (await pin_client.get(path, headers=await _locked(auth))).text()
    assert "Dinner" not in html and "data-say=" not in html
    assert snapshots.calls == []


async def test_picture_page_message_tiles(client, auth, snapshots):
    snapshots.pages["u-leo"] = leo_page(now=NOW, messages=MESSAGES)
    resp = await _get(client, auth, "/p/u-leo")
    html = await resp.text()
    _assert_board_csp(resp, html)
    # Faces and 💬, plus one small line of each message for a grown-up
    # reading along (the whole sentence stays in data-say).
    _assert_only_names_and_icons(html, PEOPLE)
    msgs = _pv_section(html, "dash-pv-msgs")
    assert _captions(msgs) == [
        'Dinner at 7 <b>"sharp"</b> @Pascal',
        "📷 Photo",
        "🎤 Can I go to Lena's?",
    ]
    assert "&lt;b&gt;" in msgs and "<b>" not in msgs  # escaped, never markup
    # The caption sits inside the tap target, under the face.
    tile = msgs[msgs.index('data-msg="m1"') :]
    assert tile.index("dash-pv-cap") < tile.index("</button>")
    # In the top row, after NOW ➜ NEXT.
    head = html[html.index('<header class="dash-pv-head">') : html.index("</header>")]
    assert head.index("dash-pv-nownext") < head.index('class="dash-pv-msgs"')
    assert head.count('<button type="button" class="dash-pv-msg') == 3
    assert 'aria-label="Message from Anna"' in head and _SAY_ANNA in head
    assert 'aria-label="Message from Pascal"' in head
    assert "dash-face dash-face-lg" in head
    # No ring on message tiles (the page owner's face wears it): a new one
    # gets a filled 💬 badge, the older ones the plain 💬.
    assert "dash-pv-current" not in head[head.index('class="dash-pv-msgs"') :]
    new = '<span class="dash-pv-msg-badge dash-pv-msg-badge-new" aria-hidden="true">💬</span>'
    plain = '<span class="dash-pv-msg-badge" aria-hidden="true">💬</span>'
    assert head.count(new) == 1 and head.count(plain) == 2
    tile = head[head.index('data-msg="m1"') :]
    assert tile.index(new) < tile.index("</button>")
    # The tile itself is the affordance: no idle 🔊 (only while speaking,
    # shown by the script); Home rows keep theirs.
    assert "🔊" not in head
    assert '<span class="dash-msg-speaker" aria-hidden="true">🔊</span>' not in head


async def test_picture_page_messages_without_now_or_next(client, auth, snapshots):
    snapshots.pages["u-leo"] = leo_page(
        now_item=None, next_item=None, messages=MESSAGES[:1]
    )
    html = await (await _get(client, auth, "/p/u-leo")).text()
    assert "dash-pv-nownext" not in html
    assert '<div class="dash-pv-top">' in html and 'data-msg="m1"' in html


async def test_picture_page_message_captions_are_one_short_line(
    client, auth, snapshots
):
    long = message("m9", "u-anna", 2, text="Please bring " + "the blue bag " * 10)
    snapshots.pages["u-leo"] = leo_page(now=NOW, messages=(long,))
    html = await (await _get(client, auth, "/p/u-leo")).text()
    (cap,) = _captions(_pv_section(html, "dash-pv-msgs"))
    assert cap.startswith("Please bring the blue bag") and cap.endswith("…")
    assert len(cap) <= 40
    # The whole message is still read aloud on a tap.
    assert "the blue bag " * 10 in unescape(_attr(html, "data-say"))


async def test_picture_page_shows_no_caption_for_senders_not_on_the_page(
    client, auth, snapshots
):
    # The snapshot leaves hidden and guardian-withheld senders out of
    # ``people``; their messages and mentions never reach a caption.
    snapshots.pages["u-leo"] = leo_page(
        now=NOW, people=(PASCAL, LEO), messages=MESSAGES, mentions=MENTIONS
    )
    html = await (await _get(client, auth, "/p/u-leo")).text()
    assert _captions(_pv_section(html, "dash-pv-msgs")) == ["📷 Photo"]
    for text in ("Dinner at 7", "Lena", "Dinner is", "call grandma"):
        assert text not in html, text


async def test_reader_page_never_shows_messages(client, auth, snapshots):
    snapshots.pages["u-anna"] = person(messages=MESSAGES)
    html = await (await _get(client, auth, "/p/u-anna")).text()
    assert "data-say=" not in html and "Dinner at 7" not in html


# ─── Household-chat mentions: @here banner, @name badge + card ───────────


def _banner(html: str) -> str:
    start = html.index('class="dash-alerts"')
    return html[start : html.index("</section>", start)]


async def test_home_here_banner(client, auth, snapshots):
    snapshots.home_snapshot = replace(snapshots.home_snapshot, mentions=MENTIONS)
    resp = await _get(client, auth, "/")
    html = await resp.text()
    _assert_board_csp(resp, html)
    banner = _banner(html)
    # Above everything else on Home.
    assert html.index('class="dash-alerts"') < html.index('<header class="dash-top">')
    assert "Message for everyone" in banner
    # Sender face + name, the text (escaped), when.
    assert "dash-face dash-face-msg" in banner and ">Anna<" in banner
    assert "@here Dinner is &lt;b&gt;ready&lt;/b&gt;!" in banner
    assert "<b>ready" not in html
    assert "data-rel>3 min ago</time>" in banner
    # A real form, so it works without JS: message id, no person.
    assert '<form class="dash-ack-form" method="post" action="/ack" data-ack>' in banner
    assert '<input type="hidden" name="message_id" value="h1">' in banner
    assert 'name="person_id"' not in banner
    assert '<span aria-hidden="true">✓</span> Got it</button>' in banner
    # Person mentions are not on Home (only their badges).
    assert "shoes on" not in html and "call grandma" not in html


async def test_home_without_here_has_no_banner(client, auth, snapshots):
    snapshots.home_snapshot = replace(
        snapshots.home_snapshot, mentions=(FOR_LEO, FOR_MIA)
    )
    html = await (await _get(client, auth, "/")).text()
    assert "dash-alerts" not in html and 'action="/ack"' not in html


def _badge(html: str, user_id: str) -> str:
    match = re.search(
        rf'<span class="dash-rail-badge" data-badge="{user_id}"[^>]*>', html
    )
    assert match, user_id
    return match.group(0)


async def test_rail_badges_people_with_a_mention_waiting(client, auth, snapshots):
    snapshots.home_snapshot = replace(snapshots.home_snapshot, mentions=MENTIONS)
    html = await (await _get(client, auth, "/")).text()
    assert not _badge(html, "u-leo").endswith(" hidden>")
    assert not _badge(html, "u-mia").endswith(" hidden>")
    assert _badge(html, "u-anna").endswith(" hidden>")  # @here badges nobody
    assert _badge(html, "u-pascal").endswith(" hidden>")
    assert '<span class="dash-visually-hidden">New message for Leo</span>' in html


async def test_mentions_swap_in_without_a_reload(client, auth, snapshots):
    page = await (await _get(client, auth, "/")).text()
    assert _attr(page, "data-mention-badges") == ""
    snapshots.home_snapshot = replace(snapshots.home_snapshot, mentions=MENTIONS)
    frag = await (await _get(client, auth, "/fragment/home")).text()
    # Same view key: the banner swaps in; the script re-badges the rail
    # and chimes from the marker.
    assert _attr(frag, "data-view-key") == _attr(page, "data-view-key")
    assert 'class="dash-alerts"' in frag
    assert _attr(frag, "data-mention-badges") == "u-leo u-mia"
    assert _attr(frag, "data-mention-keys") == "h1:here p1:u-leo p2:u-mia"
    assert _attr(frag, "data-mention-sound") == "1"


async def test_only_rendered_mentions_are_listed_for_the_tone(client, auth, snapshots):
    """A 4th @here has no banner (three at most), so it must not chime;
    a person page shows no @here at all."""
    heres = tuple(
        mention(f"h{n}", "u-anna", n, text=f"@here number {n}") for n in range(1, 5)
    )
    snapshots.home_snapshot = replace(
        snapshots.home_snapshot, mentions=(*heres, FOR_LEO)
    )
    html = await (await _get(client, auth, "/")).text()
    assert _banner(html).count('<li class="dash-alert">') == 3
    assert "number 4" not in html
    assert _attr(html, "data-mention-keys") == "h1:here h2:here h3:here p1:u-leo"
    snapshots.pages["u-anna"] = person(mentions=(*heres, FOR_LEO))
    html = await (await _get(client, auth, "/p/u-anna")).text()
    assert _attr(html, "data-mention-keys") == "p1:u-leo"


async def test_without_a_rail_only_the_own_mention_is_listed(client, auth, snapshots):
    """One shown person: no rail, so no badges — only their own card counts."""
    from_anna = mention("p5", "u-anna", 2, person="u-anna", text="@anna hi")
    snapshots.pages["u-anna"] = person(people=(ANNA,), mentions=(from_anna,))
    html = await (await _get(client, auth, "/p/u-anna")).text()
    assert "dash-rail-badge" not in html
    assert _attr(html, "data-mention-badges") == ""
    assert _attr(html, "data-mention-keys") == "p5:u-anna"


async def test_mention_sound_follows_the_admin_setting(
    client, auth, snapshots, settings_service
):
    await settings_service.update(actor_is_admin=True, patch={"mention_sound": False})
    snapshots.home_snapshot = replace(snapshots.home_snapshot, mentions=MENTIONS)
    html = await (await _get(client, auth, "/")).text()
    assert _attr(html, "data-mention-sound") == "0"


async def test_no_mentions_at_night(client, auth, snapshots):
    snapshots.home_snapshot = replace(
        snapshots.home_snapshot, is_night=True, now=at(22, 47), mentions=MENTIONS
    )
    html = await (await _get(client, auth, "/")).text()
    assert "dash-alerts" not in html and "Dinner is" not in html
    assert _attr(html, "data-mention-keys") == ""
    assert _attr(html, "data-mention-badges") == ""
    snapshots.pages["u-leo"] = leo_page(is_night=True, mentions=MENTIONS)
    html = await (await _get(client, auth, "/p/u-leo")).text()
    assert "dash-pv-mentions" not in html and "shoes on" not in html


@pytest.mark.parametrize(
    "path", ["/", "/fragment/home", "/p/u-leo", "/fragment/p/u-leo"]
)
async def test_a_locked_tablet_shows_no_mention(pin_client, auth, snapshots, path):
    snapshots.home_snapshot = replace(snapshots.home_snapshot, mentions=MENTIONS)
    snapshots.pages["u-leo"] = leo_page(mentions=MENTIONS)
    html = await (await pin_client.get(path, headers=await _locked(auth))).text()
    assert "Dinner is" not in html and "shoes on" not in html
    # No tone marker, no badge, no form.
    assert "data-mention" not in html and "dash-rail-badge" not in html
    assert 'action="/ack"' not in html
    assert snapshots.calls == []


async def test_reader_page_mention_card(client, auth, snapshots):
    for_anna = mention("p9", "u-pascal", 5, person="u-anna", text="@anna keys?")
    snapshots.pages["u-anna"] = person(mentions=(HERE, for_anna, FOR_LEO))
    resp = await _get(client, auth, "/p/u-anna")
    html = await resp.text()
    _assert_board_csp(resp, html)
    start = html.index('class="dash-for-you"')
    card = html[start : html.index("</section>", start)]
    assert html.index('class="dash-person-head"') < start
    assert "Message for you" in card and ">Pascal<" in card
    assert "@anna keys?" in card
    assert '<input type="hidden" name="message_id" value="p9">' in card
    assert '<input type="hidden" name="person_id" value="u-anna">' in card
    assert "Got it</button>" in card
    # Nothing of the others' mentions on Anna's page (only rail badges),
    # and no @here banner (that is Home's).
    assert "shoes on" not in html and "Dinner is" not in html
    assert "dash-alerts" not in html
    assert not _badge(html, "u-leo").endswith(" hidden>")
    assert not _badge(html, "u-anna").endswith(" hidden>")


async def test_picture_page_mention_tile(client, auth, snapshots):
    snapshots.pages["u-leo"] = leo_page(now=NOW, mentions=MENTIONS)
    resp = await _get(client, auth, "/p/u-leo")
    html = await resp.text()
    _assert_board_csp(resp, html)
    # The face, 💬, ✓ and one small line of the text.
    _assert_only_names_and_icons(html, PEOPLE)
    start = html.index('class="dash-pv-mentions"')
    tile = html[start : html.index("</section>", start)]
    assert 'aria-label="Message from Pascal for you"' in tile
    assert 'data-say="Pascal says: @leo shoes on"' in tile
    assert 'data-msg="mention-p1"' in tile
    assert _captions(tile) == ["@leo shoes on"]
    assert tile.index("dash-pv-cap") < tile.index("</button>")
    assert "💬" in tile and "dash-face dash-face-lg" in tile
    assert '<input type="hidden" name="person_id" value="u-leo">' in tile
    assert 'aria-label="Got it"' in tile
    assert '<span aria-hidden="true">✓</span></button>' in tile
    # Only Leo's own mention.
    assert "mention-h1" not in html and "mention-p2" not in html


async def test_picture_page_mention_without_now_or_next(client, auth, snapshots):
    snapshots.pages["u-leo"] = leo_page(
        now_item=None, next_item=None, mentions=(FOR_LEO,)
    )
    html = await (await _get(client, auth, "/p/u-leo")).text()
    assert '<div class="dash-pv-top">' in html and "dash-pv-mentions" in html


# ─── POST /ack ───────────────────────────────────────────────────────────


async def _ack(client, headers, data, page: str = "/"):
    return await client.post(
        "/ack",
        data=data,
        headers={**headers, **_origin(client, page)},
        allow_redirects=False,
    )


async def test_ack_here_redirects_home(client, auth, snapshots):
    snapshots.ackable = {("h1", None)}
    resp = await _ack(client, await _cookie(auth), {"message_id": "h1"})
    assert resp.status == 303 and resp.headers["Location"] == "/"
    assert snapshots.acks == [("h1", None)]


async def test_ack_on_a_person_page_redirects_back_there(client, auth, snapshots):
    snapshots.ackable = {("p1", "u-leo")}
    resp = await _ack(
        client,
        await _cookie(auth),
        {"message_id": "p1", "person_id": "u-leo"},
        page="/p/u-leo",
    )
    assert resp.status == 303 and resp.headers["Location"] == "/p/u-leo"
    assert snapshots.acks == [("p1", "u-leo")]


@pytest.mark.parametrize(
    "data",
    [
        {"message_id": "h1", "person_id": "u-leo"},  # not Leo's mention
        {"message_id": "nope"},
        {"message_id": "p1"},  # a person mention is no @here
        {},
        {"message_id": ""},
        {"message_id": "x" * 300},
        {"message_id": "h1", "person_id": "x" * 300},
    ],
)
async def test_ack_anything_else_is_a_plain_404(client, auth, snapshots, data):
    snapshots.ackable = {("p1", "u-leo"), ("h1", None), ("x" * 300, None)}
    resp = await _ack(client, await _cookie(auth), data)
    assert resp.status == 404
    assert "Location" not in resp.headers
    assert snapshots.acks == []


async def test_ack_never_redirects_anywhere_but_home_or_the_page(
    client, auth, snapshots
):
    snapshots.ackable = {("p1", "u-leo"), ("h1", None)}
    resp = await _ack(
        client,
        await _cookie(auth),
        {"message_id": "p1", "person_id": "u-leo", "next": "//evil.example"},
    )
    assert resp.headers["Location"] == "/p/u-leo"
    resp = await _ack(
        client,
        await _cookie(auth),
        {"message_id": "h1", "person_id": "", "next": "https://evil.example"},
    )
    assert resp.status == 303 and resp.headers["Location"] == "/"


async def test_ack_without_a_session_is_401(client, snapshots):
    snapshots.ackable = {("h1", None)}
    resp = await _ack(client, {}, {"message_id": "h1"})
    assert resp.status == 401
    assert snapshots.acks == []


async def test_ack_from_a_foreign_origin_is_403(client, auth, snapshots):
    snapshots.ackable = {("h1", None)}
    resp = await client.post(
        "/ack",
        data={"message_id": "h1"},
        headers={**await _cookie(auth), "Origin": "http://evil.example"},
        allow_redirects=False,
    )
    assert resp.status == 403
    assert snapshots.acks == []


async def test_ack_on_a_locked_tablet_is_423(pin_client, auth, snapshots):
    snapshots.ackable = {("h1", None)}
    resp = await _ack(pin_client, await _locked(auth), {"message_id": "h1"})
    assert resp.status == 423
    assert snapshots.calls == [] and snapshots.acks == []


async def test_ack_is_rate_limited_per_tablet(client, auth, snapshots):
    snapshots.ackable = {("h1", None)}
    headers = await _cookie(auth)
    for _ in range(ACK_MAX):
        assert (await _ack(client, headers, {"message_id": "h1"})).status == 303
    resp = await _ack(client, headers, {"message_id": "h1"})
    assert resp.status == 429
    assert 0 < int(resp.headers["Retry-After"]) <= ACK_WINDOW_S
    assert len(snapshots.acks) == ACK_MAX


async def test_get_ack_goes_home(client, auth, snapshots):
    resp = await client.get("/ack", headers=await _cookie(auth), allow_redirects=False)
    assert resp.status == 303 and resp.headers["Location"] == "/"
    assert snapshots.calls == []


# ─── Home tabs: Overview · Messages · Notes ─────────────────────────────

#: Anna's @here (on a banner while waiting) is a chat message too.
HERE_MSG = message("h1", "u-anna", 3, text="@here Dinner is <b>ready</b>!")
LONG_MSG = message("m4", "u-pascal", 200, text="Remember the plumber " * 20)
EVENING_MSG = replace(
    message("y1", "u-mia", 0, text="Movie after dinner?"),
    sent_at=at(21, 10, TODAY - timedelta(days=1)),
)
CHAT = (HERE_MSG, *MESSAGES, LONG_MSG, EVENING_MSG)
NOTES = tuple(
    DashboardSticky(id=f"n{i}", content=f"Note number {i}", color="#FFF9B1")
    for i in range(1, 7)
)


def _tabs_on(snapshots, **kw) -> None:
    snapshots.home_snapshot = replace(snapshots.home_snapshot, messages_on=True, **kw)


def _tab_bar(html: str) -> str:
    start = html.index('<nav class="dash-tabs"')
    return html[start : html.index("</nav>", start)]


def _current_tab(html: str) -> str:
    match = re.search(
        r'<a class="dash-tab" href="([^"]+)" aria-current="page">', _tab_bar(html)
    )
    assert match
    return match.group(1)


def _main(html: str) -> str:
    return html[html.index("<main") : html.index("</main>")]


@pytest.mark.parametrize("path", ["/", "/messages", "/notes"])
async def test_every_home_tab_has_the_bar(client, auth, snapshots, path):
    _tabs_on(snapshots)
    resp = await _get(client, auth, path)
    assert resp.status == 200
    html = await resp.text()
    _assert_board_csp(resp, html)
    bar = _tab_bar(html)
    assert '<nav class="dash-tabs" aria-label="Home">' in html
    assert [m for m in re.findall(r'href="([^"]+)"', bar)] == [
        "/",
        "/messages",
        "/notes",
    ]
    assert ">Overview<" in bar and ">Messages<" in bar and ">Notes<" in bar
    assert _current_tab(html) == path
    assert bar.count('aria-current="page"') == 1
    # The tabs are views inside Home: the rail keeps Home current.
    assert re.search(r'<a class="dash-rail-item" href="/" aria-current="page">', html)
    # The small clock on Messages and Notes; Overview has the big one.
    small = '<time class="dash-tabs-clock" datetime='
    assert (small in bar) is (path != "/")
    if path != "/":
        assert "data-clock>14:32</time>" in bar


async def test_tab_pages_wire_their_own_fragment_and_idle_return(
    client, auth, snapshots, settings_service
):
    await settings_service.update(
        actor_is_admin=True, patch={"idle_return_minutes": 10}
    )
    _tabs_on(snapshots)
    html = await (await _get(client, auth, "/")).text()
    assert 'data-fragment="/fragment/home"' in html
    assert 'data-idle-minutes="0"' in html
    for path, fragment in (
        ("/messages", "/fragment/home/messages"),
        ("/notes", "/fragment/home/notes"),
    ):
        html = await (await _get(client, auth, path)).text()
        assert f'data-fragment="{fragment}"' in html
        assert 'data-idle-minutes="10"' in html  # back to Overview


async def test_no_tab_bar_with_only_overview(client, auth, snapshots):
    snapshots.home_snapshot = replace(
        snapshots.home_snapshot,
        messages_on=False,
        sections=snapshots.home_snapshot.sections - {"stickies"},
    )
    html = await (await _get(client, auth, "/")).text()
    assert "dash-tabs" not in html and 'href="/messages"' not in html
    for path in ("/messages", "/notes"):
        resp = await _get(client, auth, path)
        assert resp.status == 404
        assert "This page isn&#39;t on the dashboard." in await resp.text()
    for path in ("/fragment/home/messages", "/fragment/home/notes"):
        resp = await _get(client, auth, path)
        assert resp.status == 404 and "<" not in await resp.text()


async def test_messages_tab_needs_the_opt_in(client, auth, snapshots):
    """Notes on, messages off: Overview · Notes, and /messages is 404."""
    html = await (await _get(client, auth, "/")).text()
    assert 'href="/messages"' not in html
    assert re.findall(r'href="([^"]+)"', _tab_bar(html)) == ["/", "/notes"]
    assert (await _get(client, auth, "/messages")).status == 404
    assert (await _get(client, auth, "/notes")).status == 200


async def test_notes_tab_needs_the_stickies_section(client, auth, snapshots):
    _tabs_on(snapshots, sections=snapshots.home_snapshot.sections - {"stickies"})
    html = await (await _get(client, auth, "/messages")).text()
    assert re.findall(r'href="([^"]+)"', _tab_bar(html)) == ["/", "/messages"]
    assert (await _get(client, auth, "/notes")).status == 404


async def test_tab_views_never_share_a_view_key(client, auth, snapshots):
    _tabs_on(snapshots)
    keys = set()
    for path, fragment in (
        ("/", "/fragment/home"),
        ("/messages", "/fragment/home/messages"),
        ("/notes", "/fragment/home/notes"),
    ):
        page = await (await _get(client, auth, path)).text()
        resp = await _get(client, auth, fragment)
        assert resp.status == 200
        frag = await resp.text()
        assert frag.lstrip().startswith('<div class="dash-view" data-view-key=')
        assert "<html" not in frag and 'class="dash-rail' not in frag
        assert _attr(frag, "data-view-key") == _attr(page, "data-view-key")
        # The fragment keeps the bar (its badge follows the refresh).
        assert _current_tab(frag) == path
        keys.add(_attr(page, "data-view-key"))
    assert len(keys) == 3


async def test_messages_badge_counts_new_messages_off_its_tab(client, auth, snapshots):
    _tabs_on(snapshots, messages=CHAT)  # h1 and m1 are under 15 min old
    for path in ("/", "/notes"):
        bar = _tab_bar(await (await _get(client, auth, path)).text())
        assert '<span class="dash-tab-badge" aria-hidden="true">2</span>' in bar
        assert '<span class="dash-visually-hidden">, 2 new</span>' in bar
    bar = _tab_bar(await (await _get(client, auth, "/messages")).text())
    assert "dash-tab-badge" not in bar and " new<" not in bar


async def test_badge_never_counts_a_bannered_message(client, auth, snapshots):
    """Review: a lone @here showed badge "1" next to an empty Messages
    tab that said there were no messages."""
    _tabs_on(snapshots, messages=(HERE_MSG,), mentions=(HERE,))
    assert "dash-tab-badge" not in await (await _get(client, auth, "/")).text()
    html = await (await _get(client, auth, "/messages")).text()
    assert "Dinner is" in _banner(html)
    # The banner shows the only message: no "No messages" claim under it.
    assert "No messages in the last day" not in html
    assert "dash-tab-empty" not in html
    # With m1 too, only m1 counts.
    _tabs_on(snapshots, messages=(HERE_MSG, *MESSAGES), mentions=(HERE,))
    bar = _tab_bar(await (await _get(client, auth, "/notes")).text())
    assert '<span class="dash-tab-badge" aria-hidden="true">1</span>' in bar


async def test_no_badge_without_new_messages(client, auth, snapshots):
    _tabs_on(snapshots, messages=(EVENING_MSG,))
    assert "dash-tab-badge" not in await (await _get(client, auth, "/")).text()


@pytest.mark.parametrize("path", ["/", "/messages", "/notes"])
async def test_here_banner_on_every_tab(client, auth, snapshots, path):
    _tabs_on(snapshots, messages=CHAT, mentions=MENTIONS)
    html = await (await _get(client, auth, path)).text()
    banner = _banner(html)
    assert "@here Dinner is &lt;b&gt;ready&lt;/b&gt;!" in banner
    assert "Got it</button>" in banner
    # Under the bar, after the offline banner, in one slot.
    slot = html.index('<div class="dash-alert-slot">')
    assert html.index('<nav class="dash-tabs"') < slot
    assert slot < html.index("data-offline") < html.index('class="dash-alerts"')
    # The tone marker lists what this page shows (the banner, the badges).
    assert _attr(html, "data-mention-keys") == "h1:here p1:u-leo p2:u-mia"


async def test_bannered_here_is_left_out_of_the_lists(client, auth, snapshots):
    _tabs_on(snapshots, messages=CHAT, mentions=MENTIONS)
    html = await (await _get(client, auth, "/")).text()
    card = html[html.index('class="dash-msgs"') : html.index("</header>")]
    # The newest three minus the banner's: not backfilled.
    assert re.findall(r'data-msg="([^"]+)"', card) == ["m1", "m2"]
    html = await (await _get(client, auth, "/messages")).text()
    listed = re.findall(r'data-msg="([^"]+)"', _main(html))
    assert "h1" not in listed and listed[0] == "m1"
    # After "Got it" it is back in its place in time order.
    _tabs_on(snapshots, messages=CHAT, mentions=(FOR_LEO,))
    html = await (await _get(client, auth, "/")).text()
    card = html[html.index('class="dash-msgs"') : html.index("</header>")]
    assert re.findall(r'data-msg="([^"]+)"', card) == ["h1", "m1", "m2"]
    html = await (await _get(client, auth, "/messages")).text()
    assert re.findall(r'data-msg="([^"]+)"', _main(html))[:2] == ["h1", "m1"]


async def test_messages_tab(client, auth, snapshots):
    _tabs_on(snapshots, messages=CHAT)
    resp = await _get(client, auth, "/messages")
    html = await resp.text()
    _assert_board_csp(resp, html)
    main = _main(html)
    assert '<h1 class="dash-visually-hidden">Messages</h1>' in main
    # Newest first, grouped by the household's day.
    assert '<h2 class="dash-chat-label" id="dash-day-today">Today</h2>' in main
    assert '<h2 class="dash-chat-label" id="dash-day-yesterday">Yesterday</h2>' in main
    assert main.index("dash-day-today") < main.index('data-msg="m4"')
    assert main.index('data-msg="m4"') < main.index("dash-day-yesterday")
    assert main.index("dash-day-yesterday") < main.index('data-msg="y1"')
    assert re.findall(r'data-msg="([^"]+)"', main) == [
        "h1",
        "m1",
        "m2",
        "m3",
        "m4",
        "y1",
    ]
    # One toggle button per row; the escaped sentence to speak.
    assert main.count('<button type="button" class="dash-chat-msg') == 6
    assert main.count('aria-pressed="false"') == 6
    assert _SAY_ANNA in main and "<b>" not in html
    assert 'data-say-playing="Reading Anna&#39;s message aloud"' in main
    row = main[main.index('data-msg="m1"') : main.index('data-msg="m2"')]
    assert 'class="dash-chat-msg dash-msg-new"' in main
    # New: the chip, but an absolute time (a stale board stays truthful).
    assert '<span class="dash-tag dash-tag-now">new</span>' in row
    assert '<time datetime="' in row and ">14:28</time>" in row
    assert "data-rel" not in row
    assert '<span class="dash-chat-who">Anna</span>' in row
    assert "dash-face dash-face-chat" in row
    assert '<span class="dash-chat-speaker" aria-hidden="true">🔊</span>' in row
    # A long one is cut on a word, with the cue; the whole is spoken.
    row = main[main.index('data-msg="m4"') : main.index('data-msg="y1"')]
    shown = re.search(r"Message from Pascal:</span> ([^<]*)</span>", row)
    assert shown is not None and shown.group(1).endswith("…")
    assert len(unescape(shown.group(1))) <= 220
    assert (
        '<span class="dash-chat-rest"><span aria-hidden="true">🔊</span> '
        "tap to hear the rest</span>" in row
    )
    whole = "Remember the plumber " * 20
    assert f'data-say="Pascal says: {whole}"' in main
    # The polite live region the script writes "Reading …" into.
    assert (
        '<p class="dash-visually-hidden" aria-live="polite" data-say-status></p>'
        in html
    )
    assert '<span id="dash-say-hint" hidden>Read aloud</span>' in main


async def test_messages_tab_same_privacy_filters_as_overview(client, auth, snapshots):
    """A sender the page doesn't show (hidden, or held back by a guardian
    block — the read model never hands those over) stays off the tab."""
    stranger = message("x1", "u-hidden", 2, text="secret from a hidden member")
    _tabs_on(snapshots, messages=(stranger, *MESSAGES))
    html = await (await _get(client, auth, "/messages")).text()
    assert "secret from a hidden member" not in html
    assert re.findall(r'data-msg="([^"]+)"', _main(html)) == ["m1", "m2", "m3"]


async def test_messages_tab_keeps_its_window_even_past_the_read(
    client, auth, snapshots
):
    """Defence in depth: more than 30, or older than a day, never lists
    or counts, whatever the snapshot hands over."""
    flood = tuple(message(f"f{i}", "u-anna", i + 1, text="hi") for i in range(35))
    stale = message("stale", "u-anna", 24 * 60 + 5, text="old news")
    _tabs_on(snapshots, messages=(*flood, stale))
    html = await (await _get(client, auth, "/messages")).text()
    listed = re.findall(r'data-msg="([^"]+)"', _main(html))
    assert len(listed) == 30 and "stale" not in listed


async def test_messages_tab_empty_state(client, auth, snapshots):
    _tabs_on(snapshots, messages=())
    html = await (await _get(client, auth, "/messages")).text()
    main = _main(html)
    assert '<div class="dash-tab-empty">' in main
    assert '<span class="dash-tab-empty-icon" aria-hidden="true">💬</span>' in main
    assert ">No messages in the last day<" in main
    assert ">Write one in the Social Home app.<" in main
    assert "dash-chat-list" not in main
    # The tab stays, so the bar never jumps.
    assert _current_tab(html) == "/messages"


async def test_notes_tab(client, auth, snapshots):
    _tabs_on(snapshots, stickies=NOTES, stickies_total=6)
    resp = await _get(client, auth, "/notes")
    html = await resp.text()
    _assert_board_csp(resp, html)
    main = _main(html)
    assert '<h1 class="dash-visually-hidden">Notes</h1>' in main
    assert '<ul class="dash-note-board">' in main
    assert main.count('<li class="dash-note k') == 6
    for i in range(1, 7):
        assert f"Note number {i}" in main
    # Warm ink for note cards only, on the server-mixed fill: --kn / --kb
    # in the style, --kt unchanged.
    style = _board_style(html)[0]
    assert re.search(r"\{--kc:#fff9b1;--kt:#000000;--kn:#1f1d1a;--kb:#", style)


async def test_notes_tab_empty_state(client, auth, snapshots):
    _tabs_on(snapshots, stickies=(), stickies_total=0)
    html = await (await _get(client, auth, "/notes")).text()
    main = _main(html)
    assert '<span class="dash-tab-empty-icon" aria-hidden="true">📝</span>' in main
    assert ">No notes yet<" in main and ">Add one in the Social Home app.<" in main
    assert "dash-note-board" not in main


async def test_overview_shows_three_notes_and_a_see_all(client, auth, snapshots):
    _tabs_on(snapshots, stickies=NOTES, stickies_total=6)
    html = await (await _get(client, auth, "/")).text()
    col = html[html.index('id="dash-notes-label"') :]
    col = col[: col.index("</section>")]
    assert ">Notes · 6</h2>" in col
    assert col.count('<li class="dash-sticky') == 3
    assert "Note number 3" in col and "Note number 4" not in col
    assert (
        '<a class="dash-see-all" href="/notes" aria-describedby="dash-notes-label">'
        "See all" in html
    )
    _tabs_on(snapshots, stickies=NOTES[:3], stickies_total=3)
    html = await (await _get(client, auth, "/")).text()
    assert 'href="/notes" aria-describedby' not in html
    assert ">Notes · 3</h2>" in html


async def test_notes_count_is_the_true_total(client, auth, snapshots):
    """Review: past the wall's hard bound the label still tells the
    truth, and the Notes tab says how many more the app holds."""
    many = tuple(
        DashboardSticky(id=f"n{i}", content=f"Note number {i}", color="#FFF9B1")
        for i in range(100)
    )
    _tabs_on(snapshots, stickies=many, stickies_total=120)
    html = await (await _get(client, auth, "/")).text()
    assert ">Notes · 120</h2>" in html
    main = _main(await (await _get(client, auth, "/notes")).text())
    assert main.count('<li class="dash-note k') == 100
    assert '<p class="dash-more">+ 20 more</p>' in main
    _tabs_on(snapshots, stickies=NOTES, stickies_total=6)
    main = _main(await (await _get(client, auth, "/notes")).text())
    assert "dash-more" not in main


async def test_overview_messages_see_all_only_with_more(client, auth, snapshots):
    _tabs_on(snapshots, messages=CHAT)
    html = await (await _get(client, auth, "/")).text()
    head = html[html.index('class="dash-msgs"') : html.index("</header>")]
    assert '<div class="dash-card-head">' in head
    assert (
        '<a class="dash-see-all" href="/messages" aria-describedby="dash-msgs-label">'
        in head
    )
    assert '<span class="dash-see-all-arrow" aria-hidden="true">›</span>' in head
    _tabs_on(snapshots, messages=MESSAGES)  # the card shows them all
    html = await (await _get(client, auth, "/")).text()
    assert 'href="/messages" aria-describedby' not in html
    # Without the opt-in there is no Messages tab to see.
    snapshots.home_snapshot = replace(
        snapshots.home_snapshot, messages_on=False, messages=CHAT
    )
    html = await (await _get(client, auth, "/")).text()
    assert 'href="/messages"' not in html


async def test_overview_whos_home_sits_under_the_date(client, auth):
    html = await (await _get(client, auth, "/")).text()
    clock = html[html.index('<div class="dash-top-clock">') :]
    clock = clock[: clock.index('<div class="dash-top-side">')]
    assert clock.index('class="dash-date"') < clock.index('class="dash-presence"')
    assert ">Work<" in clock


@pytest.mark.parametrize("path", ["/messages", "/notes"])
async def test_tabs_go_to_overview_at_night(client, auth, snapshots, path):
    _tabs_on(snapshots, is_night=True, now=at(22, 47))
    resp = await _get(client, auth, path, allow_redirects=False)
    assert resp.status == 303 and resp.headers["Location"] == "/"
    html = await (await _get(client, auth, "/")).text()
    assert "dash-night-view" in html


def test_a_woken_night_board_has_no_tab_links():
    """Review: tapped awake at night, the tab bar and "See all" links
    would only bounce back (the tabs 303 to Overview) — hidden all night,
    woken or not; the banners hide until woken like the day view."""
    css = RENDERER.static_asset("dashboard.css").body.decode()
    rule = re.search(
        r"\n(\.dash-night \.dash-tabs,\n\.dash-night \.dash-see-all) \{\n  display: none;",
        css,
    )
    assert rule is not None
    assert ".dash-night:not(.dash-woken) .dash-alerts," in css
    assert ".dash-night:not(.dash-woken) .dash-tabs," not in css


@pytest.mark.parametrize(
    "fragment", ["/fragment/home/messages", "/fragment/home/notes"]
)
async def test_night_starting_on_a_tab_reloads_it(client, auth, snapshots, fragment):
    """Night begins on a tab: the fragment's view key changes, so the
    page reloads — into the 303 to Overview."""
    _tabs_on(snapshots)
    path = fragment.removeprefix("/fragment/home")
    day = _attr(await (await _get(client, auth, path)).text(), "data-view-key")
    _tabs_on(snapshots, is_night=True, now=at(22, 47))
    frag = await (await _get(client, auth, fragment)).text()
    assert _attr(frag, "data-view-key") != day


@pytest.mark.parametrize(
    "path",
    ["/messages", "/notes", "/fragment/home/messages", "/fragment/home/notes"],
)
async def test_a_locked_tablet_shows_no_tab_content(pin_client, auth, snapshots, path):
    _tabs_on(
        snapshots, messages=CHAT, mentions=MENTIONS, stickies=NOTES, stickies_total=6
    )
    resp = await pin_client.get(path, headers=await _locked(auth))
    assert resp.status == 200
    html = await resp.text()
    for text in ("Anna", "Mia", "Dinner", "Lena", "Note number", "plumber"):
        assert text not in html, text
    if path.startswith("/fragment/"):
        assert resp.headers["X-Dashboard-Locked"] == "1"
    else:
        assert "data-lock" in html
    assert snapshots.calls == []


@pytest.mark.parametrize("path", ["/messages", "/notes"])
async def test_tab_pages_send_a_signed_out_tablet_to_login(client, path):
    resp = await client.get(path, allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == f"/login?next=%2F{path[1:]}"


# ─── Home › Map ──────────────────────────────────────────────────────────


def _map_on(snapshots, **kw) -> None:
    snapshots.home_snapshot = map_home(messages_on=True, **kw)


def _map_main(html: str) -> str:
    start = html.index('<div class="dash-mp-tab"')
    return html[start : html.index("</section>", start)]


async def test_map_tab_in_the_bar_while_someone_is_located(client, auth, snapshots):
    _map_on(snapshots)
    for path in ("/", "/messages", "/notes", "/map"):
        bar = _tab_bar(await (await _get(client, auth, path)).text())
        assert re.findall(r'href="([^"]+)"', bar) == [
            "/",
            "/messages",
            "/notes",
            "/map",
        ]
        assert ">Map<" in bar


async def test_map_page(client, auth, snapshots, settings_service):
    await settings_service.update(
        actor_is_admin=True, patch={"idle_return_minutes": 10}
    )
    _map_on(snapshots)
    resp = await _get(client, auth, "/map")
    assert resp.status == 200
    html = await resp.text()
    _assert_board_csp(resp, html)
    assert _current_tab(html) == "/map"
    assert 'data-fragment="/fragment/home/map"' in html
    assert 'data-idle-minutes="10"' in html  # back to Overview
    assert '<script src="/static/map.js" defer></script>' in html
    main = _map_main(html)
    # The list — the accessible equivalent of the map — in order.
    rows = re.findall(r'data-select="([^"]+)" aria-pressed="false"', main)
    assert rows == ["u-mia", "u-pascal", "u-leo", "u-anna"]
    assert ">Who&#39;s where</h2>" in main
    assert '<span class="dash-mp-row-name">Leo</span>' in main
    assert '<span aria-hidden="true">🏫</span> School</span>' in main
    assert '<span aria-hidden="true">🚗</span> Away</span>' in main
    assert '<span class="dash-visually-hidden">updated </span>2 min ago' in main
    # The map: one img with the spoken summary; bubbles are aria-hidden.
    assert (
        'role="img" aria-label="Map: Mia and Pascal at Home, Leo at School, '
        'Anna away"' in main
    )
    assert re.findall(r'data-bubble="([^"]+)"', main) == [
        "zone:hz_home",
        "zone:hz_school",
        "person:u-anna",
    ]
    # Zone members sit at the zone centre; only Anna's own point is drawn.
    assert 'data-lat="52.5232" data-lon="13.3961"' in main
    assert "52.5201" not in main  # Pascal's point, inside Home
    assert re.findall(r'data-zone="([^"]+)"', main).count("hz_work") == 1
    assert f'<span class="dash-mp-attrib">{MAP_ATTRIBUTION_TEXT}</span>' in main
    assert 'data-map-reset hidden><span aria-hidden="true">⤢</span> Show everyone' in (
        main
    )
    # Zone colours and person frames come from the one inline style.
    style = _board_style(html)[0]
    assert "--kc:#1f8a9e" in style and "--kf:" in style


async def test_map_fragment(client, auth, snapshots):
    _map_on(snapshots)
    page = await (await _get(client, auth, "/map")).text()
    resp = await _get(client, auth, "/fragment/home/map")
    assert resp.status == 200
    frag = await resp.text()
    assert frag.lstrip().startswith('<div class="dash-view" data-view-key=')
    assert _attr(frag, "data-view-key") == _attr(page, "data-view-key")
    assert 'data-select="u-anna"' in frag and "<html" not in frag


async def test_map_with_nobody_located_goes_to_overview(client, auth, snapshots):
    """Sharing flips at runtime: 303 to Overview, never a 404 page."""
    _tabs_on(snapshots, people=PEOPLE)
    html = await (await _get(client, auth, "/")).text()
    assert 'href="/map"' not in html
    for path in ("/map", "/fragment/home/map"):
        resp = await _get(client, auth, path, allow_redirects=False)
        assert resp.status == 303 and resp.headers["Location"] == "/"
    _map_on(snapshots, people=(located(PASCAL, 24 * 60, presence="home"),))
    resp = await _get(client, auth, "/map", allow_redirects=False)
    assert resp.status == 303


async def test_map_at_night_goes_to_overview(client, auth, snapshots):
    _map_on(snapshots, is_night=True, now=at(22, 47))
    for path in ("/map", "/fragment/home/map"):
        resp = await _get(client, auth, path, allow_redirects=False)
        assert resp.status == 303 and resp.headers["Location"] == "/"
    resp = await client.head("/map", headers=await _cookie(auth), allow_redirects=False)
    assert resp.status == 303


@pytest.mark.parametrize("path", ["/map", "/fragment/home/map"])
async def test_a_locked_map_shows_no_location(pin_client, auth, snapshots, path):
    _map_on(snapshots)
    resp = await pin_client.get(path, headers=await _locked(auth))
    assert resp.status == 200
    html = await resp.text()
    for text in ("Anna", "Mia", "Pascal", "School", "Work", "52.", "13.", "data-lat"):
        assert text not in html, text
    if path.startswith("/fragment/"):
        assert resp.headers["X-Dashboard-Locked"] == "1"
    else:
        assert "data-lock" in html
    assert snapshots.calls == []


async def test_unlock_from_the_map_lands_on_overview(pin_client, auth, snapshots):
    _map_on(snapshots)
    headers = await _locked(auth)
    resp = await _unlock(pin_client, headers)
    assert resp.status == 303 and resp.headers["Location"] == "/"


async def test_the_map_page_renews_the_cookie(pin_client, auth, clock, snapshots):
    _map_on(snapshots)
    headers = await _cookie(auth)
    clock.advance(RENEW_AFTER_S + 60)
    resp = await pin_client.get("/map", headers=headers, allow_redirects=False)
    assert resp.status == 200 and COOKIE_NAME in resp.cookies
    resp = await pin_client.head("/map", headers=headers, allow_redirects=False)
    assert resp.status == 200 and COOKIE_NAME in resp.cookies
    for path in ("/fragment/home/map", "/tiles/3/1/2"):
        resp = await pin_client.get(path, headers=headers, allow_redirects=False)
        assert COOKIE_NAME not in resp.cookies, path
    # A 303 (nobody located) renews nothing.
    _tabs_on(snapshots, people=PEOPLE)
    resp = await pin_client.get("/map", headers=headers, allow_redirects=False)
    assert resp.status == 303 and COOKIE_NAME not in resp.cookies


async def test_stale_rows_say_last_seen(client, auth, snapshots):
    _map_on(
        snapshots,
        people=(
            located(PASCAL, 130, presence="home"),
            located(MIA, 5, presence="home"),
        ),
    )
    main = _map_main(await (await _get(client, auth, "/map")).text())
    assert 'class="dash-mp-row dash-mp-stale" data-select="u-pascal"' in main
    assert '<span aria-hidden="true">⏱</span> last seen 2 h ago' in main


async def test_a_crowded_bubble_folds_into_plus_n(client, auth, snapshots):
    people = tuple(
        located(replace(p, user_id=f"u{i}", display_name=f"P{i}"), presence="home")
        for i, p in enumerate((PASCAL, PASCAL, PASCAL, PASCAL, PASCAL))
    )
    _map_on(snapshots, people=people)
    main = _map_main(await (await _get(client, auth, "/map")).text())
    bubble = main[main.index('data-bubble="zone:hz_home"') :]
    bubble = bubble[: bubble.index('<span class="dash-mp-callout')]
    assert bubble.count('data-face="') == 5
    assert bubble.count("dash-mp-extra") == 2
    assert '<span class="dash-mp-more">+2</span>' in bubble


async def test_map_markup_escapes_names(client, auth, snapshots):
    evil = located(
        replace(PASCAL, display_name='<b>"x"</b>'),
        presence="zone",
        zone_name='<i>"Gym"</i>',
        lat=52.5,
        lon=13.4,
    )
    _map_on(snapshots, people=(evil,))
    html = await (await _get(client, auth, "/map")).text()
    assert '<b>"x"</b>' not in html and "<i>" not in html
    assert "&lt;i&gt;&#34;Gym&#34;&lt;/i&gt;" in html
    assert "<b>&lt;b&gt;&#34;x&#34;&lt;/b&gt;</b>" in html  # the callout


# ─── Map tiles ───────────────────────────────────────────────────────────


async def test_tiles_come_from_the_shared_proxy(client, auth, tiles):
    resp = await _get(client, auth, "/tiles/12/2200/1343")
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "image/png"
    # Review M-2: revalidated on every use, so a lock (423) wins at once;
    # the ETag makes that a 304 from the shared in-memory tile cache.
    assert resp.headers["Cache-Control"] == "private, no-cache"
    etag = resp.headers["ETag"]
    assert await resp.read() == b"\x89PNG\r\n\x1a\ntile"
    assert tiles.calls == [(12, 2200, 1343)]
    again = await client.get(
        "/tiles/12/2200/1343", headers={**await _cookie(auth), "If-None-Match": etag}
    )
    assert again.status == 304 and await again.read() == b""
    assert again.headers["Cache-Control"] == "private, no-cache"


async def test_tiles_need_a_session(client, tiles):
    resp = await client.get("/tiles/1/0/0", allow_redirects=False)
    assert resp.status == 401  # an <img> load: no login redirect
    assert tiles.calls == []


async def test_a_locked_tablet_gets_no_tiles(pin_client, auth, tiles):
    """Tiles say which area the wall looks at — none while locked."""
    resp = await pin_client.get("/tiles/1/0/0", headers=await _locked(auth))
    assert resp.status == 423
    assert tiles.calls == []


@pytest.mark.parametrize(
    "path", ["/tiles/20/0/0", "/tiles/2/4/0", "/tiles/a/0/0", "/tiles/1/0", "/tiles/"]
)
async def test_bad_tile_coordinates_are_404(client, auth, path):
    assert (await _get(client, auth, path)).status == 404


async def test_upstream_failure_is_503(client, auth, tiles):
    tiles.fail = TileUnavailableError("down")
    resp = await _get(client, auth, "/tiles/1/0/0")
    assert resp.status == 503


async def test_tiles_are_rate_limited_per_session(client, auth, tiles):
    headers = await _cookie(auth)
    for _ in range(TILE_MAX):
        assert (await client.get("/tiles/0/0/0", headers=headers)).status == 200
    resp = await client.get("/tiles/0/0/0", headers=headers)
    assert resp.status == 429 and int(resp.headers["Retry-After"]) > 0
    # Another tablet has its own budget.
    assert (await _get(client, auth, "/tiles/0/0/0")).status == 200


async def test_no_tile_source_is_404(aiohttp_client, settings_service, auth, snapshots):
    await settings_service.set_password(actor_is_admin=True, password=PASSWORD)
    app = build_dashboard_app(
        settings_service=settings_service,
        auth=auth,
        displays=DashboardDisplayService(settings_service, auth),
        renderer=RENDERER,
        household_name=lambda: _const("Home"),
        household_tz=lambda: _const("Europe/Berlin"),
        household_theme=default_theme,
        snapshots=snapshots,
    )
    client = await aiohttp_client(app, cookie_jar=aiohttp.DummyCookieJar())
    assert (await _get(client, auth, "/tiles/0/0/0")).status == 404


async def _const(value: str) -> str:
    return value


# ─── Map scripts ─────────────────────────────────────────────────────────


def test_map_js_draws_only_from_this_origin_and_never_injects_markup():
    """The wall's map fetches nothing but this dashboard's own /tiles (the
    CSP's img-src 'self'), builds every element with createElement /
    textContent (never HTML from data), and places them through the
    CSSOM — never a style attribute, which the CSP forbids."""
    js = RENDERER.static_asset("map.js").body.decode()
    assert '"/tiles/" + frame.z + "/"' in js
    assert "http:" not in js and "https:" not in js and "//tile" not in js
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "fetch("):
        assert bad not in js, bad
    assert 'setAttribute("style"' not in js and ".cssText" not in js
    assert "el.style.left = " in js and "img.style.top = " in js
    # The framing rules of the approved design.
    assert "var MAX_ZOOM = 16;" in js and "var MIN_FIT_ZOOM = 9;" in js
    assert "var INSET_TOP = 140;" in js and "var INSET_BOTTOM = 60;" in js
    assert "var IDLE_RESET_MS = 60000;" in js and "var FACES = 3;" in js
    assert 'addEventListener("dash:swap"' in js


def test_dashboard_js_follows_a_fragment_redirect_home_and_tells_map_js():
    js = RENDERER.static_asset("dashboard.js").body.decode()
    refresh = js[js.index("function refresh()") :]
    refresh = refresh[: refresh.index("\n  }\n")]
    assert 'redirect: "manual"' in refresh
    assert 'res.type === "opaqueredirect"' in refresh
    # Review M-4: Overview never follows one (no reload loop behind a
    # redirecting proxy) — it counts as a failed refresh.
    follow = refresh[refresh.index('res.type === "opaqueredirect"') :]
    follow = follow[: follow.index("return null;")]
    assert "if (fragmentUrl === HOME_PROBE) {\n            failed();" in follow
    assert follow.index("failed();") < follow.index('go("/")')
    swap = js[js.index("function swap(") :]
    swap = swap[: swap.index("\n  }\n")]
    assert 'new window.CustomEvent("dash:swap")' in swap
    assert ".dash-face img, .dash-mp-av img" in js


async def test_only_the_map_page_loads_map_js(client, auth, snapshots):
    _map_on(snapshots)
    for path in ("/", "/messages", "/notes", "/p/u-anna"):
        assert "map.js" not in await (await _get(client, auth, path)).text()
    resp = await client.get("/static/map.js")
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith(
        ("text/javascript", "application/javascript")
    )
