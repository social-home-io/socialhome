"""Tests for the wall-panel JSON API on the dashboard port
(``/api/display/v1/…``): pairing, bearer auth, whoami.

Real aiohttp test server with the full dashboard middleware chain.
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import replace
from pathlib import Path

import aiohttp
import pytest
from jsonschema import Draft202012Validator
from PIL import Image
from referencing import Registry, Resource

import socialhome.i18n as i18n_pkg
from socialhome.dashboard.app import build_dashboard_app
from socialhome.dashboard.auth import COOKIE_NAME, PAIR_MAX, DashboardAuth
from socialhome.dashboard.display_api import (
    DISPLAY_API_PREFIX,
    bearer_token,
    is_display_api,
    is_public_display_path,
)
from socialhome.dashboard.auth import DISPLAY_IMAGE_MAX, DISPLAY_READ_MAX
from socialhome.dashboard.display_json import STRING_KEYS
from socialhome.dashboard.render import DashboardRenderer
from socialhome.domain.theme import HouseholdTheme
from socialhome.domain.household_dashboard import PAIRING_TTL_SECONDS
from socialhome.i18n import Catalog
from socialhome.services.dashboard_display_service import (
    SEEN_INTERVAL_S,
    DashboardDisplayService,
)

from .conftest import PASSWORD, default_theme
from .factories import (
    MENTIONS,
    MESSAGES,
    home,
    leo_page,
    map_home,
    person,
)

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
def theme_box() -> dict:
    """``theme_box["fn"]`` is the household theme the app reads."""
    return {"fn": default_theme}


@pytest.fixture
async def client(
    aiohttp_client, settings_service, auth, displays, snapshots, theme_box
):
    async def household_theme() -> HouseholdTheme:
        return await theme_box["fn"]()

    await settings_service.set_password(actor_is_admin=True, password=PASSWORD)
    await settings_service.update(actor_is_admin=True, patch={"enabled": True})
    app = build_dashboard_app(
        settings_service=settings_service,
        auth=auth,
        displays=displays,
        renderer=RENDERER,
        household_name=_name,
        household_tz=_tz,
        household_theme=household_theme,
        snapshots=snapshots,
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


# ── The boards as JSON ───────────────────────────────────────────────────

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "docs" / "display-api" / "v1"
FIXTURE_DIR = SCHEMA_DIR / "fixtures"
#: Set to rewrite the documented fixtures from the seeded household.
REGEN = os.environ.get("SH_UPDATE_DISPLAY_FIXTURES") == "1"


def _registry() -> Registry:
    resources = []
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


REGISTRY = _registry()


def _validate(name: str, payload: object) -> None:
    schema = json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text("utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema, registry=REGISTRY).iter_errors(payload))
    assert errors == [], [f"{list(e.absolute_path)}: {e.message}" for e in errors]


def _fixture(name: str, payload: object) -> None:
    """The documented fixture equals what the endpoint answers."""
    path = FIXTURE_DIR / f"{name}.json"
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if REGEN:
        path.write_text(text, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == text, (
        f"{path.name} is stale — rerun with SH_UPDATE_DISPLAY_FIXTURES=1"
    )


def _seed(snapshots) -> None:
    """The documented household: four people, a school day, chat on,
    an @here and two person mentions, three people located."""
    snapshots.home_snapshot = map_home(
        messages=MESSAGES, messages_on=True, mentions=MENTIONS, stickies_total=5
    )
    snapshots.pages["u-anna"] = person(mentions=MENTIONS)
    snapshots.pages["u-leo"] = leo_page(messages=MESSAGES, mentions=MENTIONS)


async def _get(client, path, token, **kw):
    return await client.get(
        path, headers={**_bearer(token), **kw.pop("headers", {})}, **kw
    )


@pytest.mark.parametrize(
    ("path", "schema", "fixture"),
    [
        ("/api/display/v1/home", "home", "home"),
        ("/api/display/v1/messages", "messages", "messages"),
        ("/api/display/v1/notes", "notes", "notes"),
        ("/api/display/v1/map", "map", "map"),
        ("/api/display/v1/person/u-anna", "person", "person-reader"),
        ("/api/display/v1/person/u-mia", "person", "person-school"),
        ("/api/display/v1/person/u-leo", "person", "person-picture"),
        ("/api/display/v1/theme", "theme", "theme"),
    ],
)
async def test_board_endpoints_match_schema_and_fixture(
    client, displays, snapshots, path, schema, fixture
):
    _seed(snapshots)
    _, token = await _paired(client, displays)
    resp = await _get(client, path, token)
    assert resp.status == 200, await resp.text()
    assert resp.content_type == "application/json"
    assert resp.headers["Cache-Control"] == "private, no-cache"
    payload = await resp.json()
    assert payload["api_version"] == 1
    _validate(schema, payload)
    _fixture(fixture, payload)


async def test_night_home_matches_schema_and_fixture(client, displays, snapshots):
    snapshots.home_snapshot = home(is_night=True, mentions=MENTIONS, messages=MESSAGES)
    _, token = await _paired(client, displays)
    payload = await (await _get(client, "/api/display/v1/home", token)).json()
    _validate("home", payload)
    assert payload["is_night"] and payload["night"]["item"]["when"] == "tomorrow"
    # Night: no banners, no badges, no messages card.
    assert payload["alerts"] == []
    assert all(p["badge"] == 0 for p in payload["people"])
    assert payload["overview"]["messages"] is None
    _fixture("home-night", payload)


async def test_night_picture_person(client, displays, snapshots):
    snapshots.pages["u-leo"] = leo_page(
        is_night=True, messages=MESSAGES, mentions=MENTIONS
    )
    _, token = await _paired(client, displays)
    payload = await (await _get(client, "/api/display/v1/person/u-leo", token)).json()
    _validate("person", payload)
    assert payload["night"] is None  # the picture view has its own night
    assert payload["picture"]["night"] is not None
    assert payload["picture"]["messages"] == [] and payload["mentions"] == []


async def test_etag_304(client, displays, snapshots):
    _seed(snapshots)
    _, token = await _paired(client, displays)
    for path in ("/api/display/v1/home", "/api/display/v1/theme"):
        first = await _get(client, path, token)
        etag = first.headers["ETag"]
        again = await _get(client, path, token, headers={"If-None-Match": etag})
        assert again.status == 304 and again.headers["ETag"] == etag
        assert await again.read() == b""
        listed = await _get(
            client, path, token, headers={"If-None-Match": f'"x", W/{etag}'}
        )
        assert listed.status == 304
        other = await _get(client, path, token, headers={"If-None-Match": '"x"'})
        assert other.status == 200


async def test_etag_changes_with_content(client, displays, snapshots):
    _, token = await _paired(client, displays)
    first = (await _get(client, "/api/display/v1/home", token)).headers["ETag"]
    snapshots.home_snapshot = home(shopping_total=99)
    second = (await _get(client, "/api/display/v1/home", token)).headers["ETag"]
    assert first != second


BOARD_PATHS = [
    "/api/display/v1/home",
    "/api/display/v1/messages",
    "/api/display/v1/notes",
    "/api/display/v1/map",
    "/api/display/v1/person/u-anna",
    "/api/display/v1/theme",
    "/api/display/v1/avatar/u-anna",
    "/api/display/v1/emoji/1f3ca",
]


@pytest.mark.parametrize("path", BOARD_PATHS)
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer " + "a" * 43}, {"Authorization": "Bearer"}],
)
async def test_board_endpoints_need_a_token(client, displays, path, headers):
    await _paired(client, displays)
    resp = await client.get(path, headers=headers)
    assert resp.status == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("path", BOARD_PATHS)
async def test_tablet_cookie_opens_no_board_endpoint(client, auth, path):
    cookie = {"Cookie": f"{COOKIE_NAME}={await auth.issue(True)}"}
    assert (await client.get(path, headers=cookie)).status == 401


@pytest.mark.parametrize("path", [*BOARD_PATHS, WHOAMI])
async def test_locked_display_gets_423_lock_payload(
    client, displays, snapshots, path, monkeypatch
):
    _seed(snapshots)
    _, token = await _paired(client, displays)
    real = DashboardDisplayService.authenticate

    async def locked(self, value):
        found = await real(self, value)
        return None if found is None else replace(found, locked=True)

    monkeypatch.setattr(DashboardDisplayService, "authenticate", locked)
    resp = await _get(client, path, token)
    assert resp.status == 423
    payload = await resp.json()
    _validate("lock", payload)
    assert payload["household"] == "Home" and payload["locked"] is True
    text = json.dumps(payload)
    for leak in ("Anna", "u-anna", "Swimming", "Dinner"):
        assert leak not in text
    snapshots.calls.clear()
    await _get(client, path, token)
    assert snapshots.calls == []  # nothing about the household was read
    if path == "/api/display/v1/home":
        _fixture("locked", payload)


async def test_tab_off_is_404_and_night_tab_is_409(client, displays, snapshots):
    _, token = await _paired(client, displays)
    snapshots.home_snapshot = home(sections=frozenset({"calendar"}))
    for tab in ("messages", "notes", "map"):
        resp = await _get(client, f"/api/display/v1/{tab}", token)
        assert resp.status == 404
        body = await resp.json()
        _validate("error", body)
        assert body["error"]["code"] == "TAB_OFF"
    snapshots.home_snapshot = map_home(is_night=True, messages_on=True)
    for tab in ("messages", "notes", "map"):
        resp = await _get(client, f"/api/display/v1/{tab}", token)
        assert resp.status == 409
        assert (await resp.json())["error"]["code"] == "NIGHT"
    assert (await _get(client, "/api/display/v1/home", token)).status == 200


async def test_unknown_or_hidden_person_is_404(client, displays):
    _, token = await _paired(client, displays)
    resp = await _get(client, "/api/display/v1/person/u-nobody", token)
    assert resp.status == 404
    assert (await resp.json())["error"]["code"] == "NOT_FOUND"


async def test_payload_never_carries_hidden_detail(client, displays, snapshots):
    """Event locations, lesson notes and shopping stores are not on the
    wall, so they are not in the JSON either."""
    _seed(snapshots)
    _, token = await _paired(client, displays)
    for path in ("/api/display/v1/home", "/api/display/v1/person/u-anna"):
        text = await (await _get(client, path, token)).text()
        assert "Secret Street" not in text
        assert '"location"' not in text and '"store"' not in text


async def test_theme_follows_household_and_locale(
    client, displays, settings_service, theme_box
):
    _, token = await _paired(client, displays)
    payload = await (await _get(client, "/api/display/v1/theme", token)).json()
    assert set(payload["strings"]) == set(STRING_KEYS)
    assert payload["strings"]["offline"].count("{time}") == 1
    # The default terracotta doesn't reach 4.5:1 on the light surfaces:
    # the built-in accent stays (as on the tablet, ``theme_css``).
    assert payload["household_colors"]["primary"] == "#d2542a"
    assert payload["colors"]["light"]["accent"] == "#2f6f5e"
    assert payload["colors"]["dark"]["bg"] == "#151412"
    assert payload["colors"]["night"]["bg"] == "#000000"

    async def navy() -> HouseholdTheme:
        return HouseholdTheme(primary_color="#1A4D8F")

    theme_box["fn"] = navy
    payload = await (await _get(client, "/api/display/v1/theme", token)).json()
    assert payload["colors"]["light"]["accent"] == "#1a4d8f"
    assert payload["colors"]["light"]["accent_text"] == "#ffffff"
    assert payload["colors"]["dark"]["accent"] == "#6cc3a8"  # unreadable on dark
    await settings_service.update(actor_is_admin=True, patch={"locale": "de"})
    payload = await (await _get(client, "/api/display/v1/theme", token)).json()
    assert payload["locale"] == "de"
    assert payload["strings"]["tab_overview"] == "Übersicht"


async def test_theme_read_failure_keeps_built_in_accent(client, displays, theme_box):
    async def broken() -> HouseholdTheme:
        raise RuntimeError("db down")

    theme_box["fn"] = broken
    _, token = await _paired(client, displays)
    payload = await (await _get(client, "/api/display/v1/theme", token)).json()
    assert payload["household_colors"]["primary"] is None
    assert payload["colors"]["light"]["accent"] == "#2f6f5e"


# ── Images ───────────────────────────────────────────────────────────────


def _webp() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (200, 150), (200, 50, 50)).save(out, format="WEBP")
    return out.getvalue()


def _png_size(body: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(body)) as img:
        assert img.format == "PNG"
        return img.size


async def test_avatar_png(client, displays, snapshots):
    snapshots.pictures["u-anna"] = (_webp(), "pic1")
    _, token = await _paired(client, displays)
    resp = await _get(client, "/api/display/v1/avatar/u-anna?size=64", token)
    assert resp.status == 200 and resp.content_type == "image/png"
    assert _png_size(await resp.read()) == (64, 64)
    assert resp.headers["ETag"] == '"pic1-64"'
    default = await _get(client, "/api/display/v1/avatar/u-anna", token)
    assert _png_size(await default.read()) == (96, 96)
    again = await _get(
        client,
        "/api/display/v1/avatar/u-anna?size=64",
        token,
        headers={"If-None-Match": '"pic1-64"'},
    )
    assert again.status == 304


async def test_avatar_rules(client, displays, snapshots):
    snapshots.pictures["u-anna"] = (b"garbage", "pic1")
    _, token = await _paired(client, displays)
    assert (await _get(client, "/api/display/v1/avatar/u-mia", token)).status == 404
    assert (
        await _get(client, "/api/display/v1/avatar/u-anna", token)
    ).status == 404  # undecodable: no picture
    for bad in ("7", "abc", "96.0", "-96", "４８"):
        resp = await _get(client, f"/api/display/v1/avatar/u-anna?size={bad}", token)
        assert resp.status == 400
        assert (await resp.json())["error"]["code"] == "BAD_SIZE"


async def test_emoji_png_and_fallback(client, displays):
    _, token = await _paired(client, displays)
    resp = await _get(client, "/api/display/v1/emoji/1f3ca?size=48", token)
    assert resp.status == 200 and resp.headers["ETag"] == '"1f3ca-48"'
    assert _png_size(await resp.read()) == (48, 48)
    native = await _get(client, "/api/display/v1/emoji/1f3ca", token)
    assert _png_size(await native.read()) == (72, 72)
    unknown = await _get(client, "/api/display/v1/emoji/10fff0", token)
    assert unknown.status == 200 and unknown.headers["ETag"] == '"1f4cd-72"'
    assert (
        await _get(client, "/api/display/v1/emoji/1f3ca?size=1000", token)
    ).status == 400
    for bad in ("..%2FLICENSE.md", "LICENSE.md", "1F3CA"):
        assert (await _get(client, f"/api/display/v1/emoji/{bad}", token)).status == 404


# ── Rate limits ──────────────────────────────────────────────────────────


async def test_reads_are_rate_limited_per_display(client, displays, auth):
    display_id, token = await _paired(client, displays)
    for _ in range(DISPLAY_READ_MAX):
        assert auth.begin_display_read(display_id) == 0
    resp = await _get(client, "/api/display/v1/home", token)
    assert resp.status == 429 and int(resp.headers["Retry-After"]) > 0
    # Images have their own budget.
    assert (await _get(client, "/api/display/v1/emoji/1f3ca", token)).status == 200
    for _ in range(DISPLAY_IMAGE_MAX):
        auth.begin_display_read(display_id, image=True)
    assert (await _get(client, "/api/display/v1/emoji/1f3ca", token)).status == 429
    # Another panel is unaffected.
    _, other = await _paired(client, displays, name="Hall")
    assert (await _get(client, "/api/display/v1/home", other)).status == 200


# ── Wording parity with the tablet ───────────────────────────────────────


async def test_json_wording_matches_the_html_board(client, displays, snapshots, auth):
    """The same household, read by a tablet (HTML) and by a panel (JSON):
    the strings the panel shows appear verbatim on the tablet's page."""
    _seed(snapshots)
    _, token = await _paired(client, displays)
    cookie = {"Cookie": f"{COOKIE_NAME}={await auth.issue(True)}"}

    async def html(path: str) -> str:
        resp = await client.get(path, headers=cookie)
        assert resp.status == 200
        return (await resp.text()).replace("&#39;", "'").replace("&amp;", "&")

    page = await html("/")
    ov = (await (await _get(client, "/api/display/v1/home", token)).json())["overview"]
    for text in (
        ov["chores"]["label"].split(" · ")[0],
        ov["shopping"]["more"],
        ov["next"]["rel"],
        ov["next"]["title"],
        ov["today"]["label"],
        ov["today"]["timeline"]["label"],
        ov["presence"]["label"],
        *(m["when"] for m in ov["messages"]["items"]),
        *(c["late"] for c in ov["chores"]["items"] if c["late"]),
    ):
        assert text in page, text
    assert ov["chores"]["label"] == "Chores today · 2 open"

    page = await html("/p/u-leo")
    pv = (await (await _get(client, "/api/display/v1/person/u-leo", token)).json())[
        "picture"
    ]
    for text in (
        pv["clock"]["label"],
        pv["now"]["caption"],
        pv["next"]["label"],
        pv["day"]["parts"][0]["tiles"][2]["label"],
        pv["jobs"]["items"][0]["label"],
    ):
        assert text in page, text

    page = await html("/map")
    mp = (await (await _get(client, "/api/display/v1/map", token)).json())["map"]
    assert mp["label"] in page
    for spot in mp["spots"]:
        assert spot["place"] in page and spot["when"] in page
