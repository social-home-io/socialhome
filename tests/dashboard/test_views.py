"""Tests for the dashboard views: login, setup link, logout, home."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

import socialhome.i18n as i18n_pkg
from socialhome.dashboard.app import build_dashboard_app, build_dashboard_csp
from socialhome.dashboard.auth import (
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
from socialhome.dashboard.views import _hostname, cookie_secure, safe_next
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

    async def tz() -> str:
        return household.get("tz", "Europe/Berlin")

    app = build_dashboard_app(
        settings_service=settings_service,
        auth=auth,
        renderer=RENDERER,
        household_name=name,
        household_tz=tz,
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
        assert "Your dashboard is ready" not in html
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
    """A dashboard with a page whose handler revokes the very cookie that
    reached it — what a ``/lock`` racing a slow page load does."""
    await settings_service.set_password(actor_is_admin=True, password=PASSWORD)
    await settings_service.set_pin(actor_is_admin=True, pin=PIN)

    async def name() -> str:
        return "Home"

    async def tz() -> str:
        return "UTC"

    app = build_dashboard_app(
        settings_service=settings_service,
        auth=auth,
        renderer=RENDERER,
        household_name=name,
        household_tz=tz,
    )

    async def race(request: web.Request) -> web.Response:
        request.app[AUTH_KEY].revoke(request[CLAIMS_KEY], request.query["why"])
        return web.Response(text="slow page")

    app.router.add_get("/race", race)
    return await aiohttp_client(app, cookie_jar=aiohttp.DummyCookieJar())


async def test_renewal_after_a_racing_lock_issues_a_locked_cookie(
    race_client, auth, clock
):
    headers = await _cookie(auth)
    clock.advance(RENEW_AFTER_S + 1)
    resp = await race_client.get(f"/race?why={REVOKED_LOCKED}", headers=headers)
    assert resp.status == 200
    assert (await _claims(auth, resp)).locked is True


async def test_renewal_after_a_racing_sign_out_issues_nothing(race_client, auth, clock):
    headers = await _cookie(auth)
    clock.advance(RENEW_AFTER_S + 1)
    resp = await race_client.get(f"/race?why={REVOKED_SIGNED_OUT}", headers=headers)
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
    assert "Your dashboard is ready" in await home.text()


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
    assert "Your dashboard is ready" in html
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
    """A dashboard whose throttle table holds one client."""
    await settings_service.set_password(actor_is_admin=True, password=PASSWORD)
    await settings_service.set_pin(actor_is_admin=True, pin=PIN)
    auth = DashboardAuth(settings_service, clock=clock, max_throttle_keys=1)

    async def name() -> str:
        return "Home"

    async def tz() -> str:
        return "UTC"

    app = build_dashboard_app(
        settings_service=settings_service,
        auth=auth,
        renderer=RENDERER,
        household_name=name,
        household_tz=tz,
    )
    client = await aiohttp_client(app, cookie_jar=aiohttp.DummyCookieJar())
    return client, auth


async def test_full_throttle_table_holds_back_but_never_signs_out(tiny_client, clock):
    """F2: only this tablet's own wrong PINs sign it out."""
    client, auth = tiny_client
    for _ in range(THROTTLE_MAX_FAILURES):
        auth.begin_attempt("someone-else", count_global=False)
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
        assert "Your dashboard is ready" in html
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
