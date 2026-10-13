"""Tests for the wall-panel JSON API on the dashboard port
(``/api/display/v1/…``): pairing, bearer auth, whoami.

Real aiohttp test server with the full dashboard middleware chain.
"""

from __future__ import annotations

from pathlib import Path

import aiohttp
import pytest

import socialhome.i18n as i18n_pkg
from socialhome.dashboard.app import build_dashboard_app
from socialhome.dashboard.auth import COOKIE_NAME, PAIR_MAX, DashboardAuth
from socialhome.dashboard.display_api import (
    DISPLAY_API_PREFIX,
    bearer_token,
    is_display_api,
    is_public_display_path,
)
from socialhome.dashboard.render import DashboardRenderer
from socialhome.domain.household_dashboard import PAIRING_TTL_SECONDS
from socialhome.i18n import Catalog
from socialhome.services.dashboard_display_service import (
    SEEN_INTERVAL_S,
    DashboardDisplayService,
)

from .conftest import PASSWORD, default_theme
from .factories import FakeDashboardService

RENDERER = DashboardRenderer(
    Catalog.from_directory(Path(i18n_pkg.__file__).parent / "messages")
)
PAIR = "/api/display/v1/pair"
WHOAMI = "/api/display/v1/whoami"


async def _name() -> str:
    return "Home"


async def _tz() -> str:
    return "UTC"


@pytest.fixture
def auth(settings_service, clock) -> DashboardAuth:
    return DashboardAuth(settings_service, clock=clock)


@pytest.fixture
def displays(settings_service, auth, clock) -> DashboardDisplayService:
    return DashboardDisplayService(settings_service, auth, clock=clock)


@pytest.fixture
async def client(aiohttp_client, settings_service, auth, displays):
    await settings_service.set_password(actor_is_admin=True, password=PASSWORD)
    await settings_service.update(actor_is_admin=True, patch={"enabled": True})
    app = build_dashboard_app(
        settings_service=settings_service,
        auth=auth,
        displays=displays,
        renderer=RENDERER,
        household_name=_name,
        household_tz=_tz,
        household_theme=default_theme,
        snapshots=FakeDashboardService(),
    )
    return await aiohttp_client(app, cookie_jar=aiohttp.DummyCookieJar())


async def _paired(client, displays, name: str = "Kitchen") -> tuple[str, str]:
    """Pair a panel over the API: ``(display_id, token)``."""
    resp = await client.post(PAIR, json={"model": "esp32-s3-box"})
    assert resp.status == 201
    started = await resp.json()
    await displays.claim(actor_is_admin=True, code=started["code"], name=name)
    resp = await client.get(f"{PAIR}/{started['pair_id']}")
    body = await resp.json()
    assert body["status"] == "paired"
    return body["display_id"], body["token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── Pure helpers ─────────────────────────────────────────────────────────


def test_path_helpers():
    assert DISPLAY_API_PREFIX == "/api/display/v1/"
    assert is_display_api("/api/display/v1/whoami")
    assert not is_display_api("/api/display/v2/whoami")
    assert not is_display_api("/api/display/v1")
    assert is_public_display_path(PAIR)
    assert is_public_display_path(f"{PAIR}/abc")
    # Only the pairing routes themselves — never anything nested below.
    assert not is_public_display_path(f"{PAIR}/abc/def")
    assert not is_public_display_path(f"{PAIR}/")
    assert not is_public_display_path(f"{PAIR}x")
    assert not is_public_display_path(WHOAMI)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("Bearer abc", "abc"),
        ("bearer  abc ", "abc"),
        ("Basic abc", None),
        ("Bearer", None),
        ("Bearer a b", None),
    ],
)
def test_bearer_token(header, expected):
    headers = {} if header is None else {"Authorization": header}
    assert bearer_token(headers) == expected


# ── Pairing ──────────────────────────────────────────────────────────────


async def test_pair_start(client):
    resp = await client.post(PAIR, json={"model": "box", "firmware": "0.1.0"})
    assert resp.status == 201
    body = await resp.json()
    assert set(body) == {"pair_id", "code", "expires_in"}
    assert body["expires_in"] == PAIRING_TTL_SECONDS
    assert resp.headers["Cache-Control"] == "no-store"


async def test_pair_start_needs_no_origin_and_no_body(client):
    """An ESPHome panel sends neither ``Origin`` nor a cookie — no CSRF
    applies (nothing ambient to ride on)."""
    resp = await client.post(PAIR)
    assert resp.status == 201


@pytest.mark.parametrize("body", [b"{nope", b"[1]", b'"x"'])
async def test_pair_start_bad_json_is_400(client, body):
    resp = await client.post(
        PAIR, data=body, headers={"Content-Type": "application/json"}
    )
    assert resp.status == 400
    assert (await resp.json())["error"]["code"] == "BAD_REQUEST"


async def test_pair_start_is_throttled_per_client(client):
    for _ in range(PAIR_MAX):
        assert (await client.post(PAIR)).status == 201
    resp = await client.post(PAIR)
    assert resp.status == 429
    assert int(resp.headers["Retry-After"]) > 0
    assert (await resp.json())["error"]["code"] == "RATE_LIMITED"


async def test_pair_start_refused_while_disabled(client, settings_service):
    await settings_service.update(actor_is_admin=True, patch={"enabled": False})
    resp = await client.post(PAIR)
    assert resp.status == 409
    assert (await resp.json())["error"]["code"] == "DASHBOARD_DISABLED"


async def test_pair_poll_pending_then_paired_once(client, displays):
    started = await (await client.post(PAIR)).json()
    resp = await client.get(f"{PAIR}/{started['pair_id']}")
    assert resp.status == 200
    assert await resp.json() == {"status": "pending"}
    await displays.claim(actor_is_admin=True, code=started["code"], name="Hall")
    resp = await client.get(f"{PAIR}/{started['pair_id']}")
    body = await resp.json()
    assert body["status"] == "paired"
    assert set(body) == {"status", "token", "display_id"}
    # Exactly once.
    resp = await client.get(f"{PAIR}/{started['pair_id']}")
    assert resp.status == 404


async def test_pair_poll_unknown_is_404(client):
    resp = await client.get(f"{PAIR}/does-not-exist")
    assert resp.status == 404
    assert (await resp.json())["error"]["code"] == "NOT_FOUND"


async def test_pair_poll_expired_is_404(client, clock):
    started = await (await client.post(PAIR)).json()
    clock.advance(PAIRING_TTL_SECONDS + 1)
    assert (await client.get(f"{PAIR}/{started['pair_id']}")).status == 404


# ── Bearer auth ──────────────────────────────────────────────────────────


async def test_whoami_with_token(client, displays):
    display_id, token = await _paired(client, displays)
    resp = await client.get(WHOAMI, headers=_bearer(token))
    assert resp.status == 200
    assert await resp.json() == {"display_id": display_id, "name": "Kitchen"}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer " + "a" * 43},
        {"Authorization": "Basic Zm9vOmJhcg=="},
    ],
)
async def test_whoami_without_valid_token_is_401(client, displays, headers):
    await _paired(client, displays)
    resp = await client.get(WHOAMI, headers=headers)
    assert resp.status == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"
    assert (await resp.json())["error"]["code"] == "UNAUTHORIZED"


async def test_tablet_cookie_gets_no_display_access(client, auth):
    cookie = {"Cookie": f"{COOKIE_NAME}={await auth.issue(True)}"}
    resp = await client.get(WHOAMI, headers=cookie)
    assert resp.status == 401


async def test_display_token_opens_no_tablet_page(client, displays):
    _, token = await _paired(client, displays)
    resp = await client.get("/", headers=_bearer(token), allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"].startswith("/login")
    resp = await client.get("/fragment/home", headers=_bearer(token))
    assert resp.status == 401


async def test_unknown_display_path_with_token_is_404(client, displays):
    _, token = await _paired(client, displays)
    resp = await client.get("/api/display/v1/nope", headers=_bearer(token))
    assert resp.status == 404


async def test_unknown_display_path_without_token_is_401(client):
    """Authenticated first: nothing about the API's shape leaks."""
    resp = await client.get("/api/display/v1/nope")
    assert resp.status == 401


async def test_removed_display_is_refused(client, displays):
    display_id, token = await _paired(client, displays)
    await displays.remove(actor_is_admin=True, display_id=display_id)
    assert (await client.get(WHOAMI, headers=_bearer(token))).status == 401


async def test_sign_out_all_tablets_revokes_the_display(
    client, displays, settings_service
):
    _, token = await _paired(client, displays)
    await settings_service.revoke_sessions(actor_is_admin=True)
    assert (await client.get(WHOAMI, headers=_bearer(token))).status == 401


async def test_bearer_post_skips_the_origin_check(client, displays):
    """A cross-origin POST is refused for cookie routes, but the display
    API carries no cookie authority — a bearer POST reaches the route
    (whoami answers only GET: 405, not the CSRF 403)."""
    _, token = await _paired(client, displays)
    resp = await client.post(WHOAMI, headers=_bearer(token))
    assert resp.status == 405


async def test_cookie_post_still_needs_same_origin(client, auth):
    cookie = {"Cookie": f"{COOKIE_NAME}={await auth.issue(True)}"}
    resp = await client.post("/lock", headers=cookie)
    assert resp.status == 403


async def test_seen_is_recorded_with_headers(client, displays, settings_service, clock):
    _, token = await _paired(client, displays)
    headers = {
        **_bearer(token),
        "X-Display-Firmware": "0.2.0",
        "X-Display-Model": "esp32-p4",
    }
    assert (await client.get(WHOAMI, headers=headers)).status == 200
    stored = (await settings_service.displays())[0]
    assert stored.last_seen_at is not None
    assert (stored.firmware, stored.model) == ("0.2.0", "esp32-p4")
    # Within the minute: not written again.
    headers["X-Display-Firmware"] = "0.3.0"
    await client.get(WHOAMI, headers=headers)
    assert (await settings_service.displays())[0].firmware == "0.2.0"
    clock.advance(SEEN_INTERVAL_S)
    await client.get(WHOAMI, headers=headers)
    assert (await settings_service.displays())[0].firmware == "0.3.0"


async def test_display_request_does_not_touch_tablet_last_seen(client, displays, auth):
    _, token = await _paired(client, displays)
    await client.get(WHOAMI, headers=_bearer(token))
    assert auth.last_seen_at is None
