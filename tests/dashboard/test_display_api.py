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
from socialhome.dashboard.auth import (
    ACK_MAX,
    COOKIE_NAME,
    PAIR_MAX,
    THROTTLE_MAX_FAILURES,
    DashboardAuth,
)
from socialhome.dashboard.display_api import (
    DISPLAY_API_PREFIX,
    bearer_token,
    is_display_api,
    is_public_display_path,
)
from socialhome.dashboard.auth import (
    DISPLAY_IMAGE_MAX,
    DISPLAY_READ_MAX,
    MAP_RENDER_MAX,
)
from socialhome.dashboard.display_json import STRING_KEYS
from socialhome.dashboard.render import DashboardRenderer
from socialhome.domain.theme import HouseholdTheme
from socialhome.domain.household_dashboard import (
    CLAIM_TTL_SECONDS,
    PAIRING_TTL_SECONDS,
    ExternalDisplayPanel,
)
from socialhome.i18n import Catalog
from socialhome.services.dashboard_display_service import (
    SEEN_INTERVAL_S,
    DashboardDisplayService,
)
from socialhome.services.dashboard_settings_service import DashboardSettingsService

from .conftest import PASSWORD, default_theme
from .factories import (
    MAP_PEOPLE,
    MENTIONS,
    PictureTiles,
    solid_png,
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
LOCK = "/api/display/v1/lock"
UNLOCK = "/api/display/v1/unlock"
ACK = "/api/display/v1/ack"
PIN = "2468"


async def _name() -> str:
    return "Home"


async def _tz() -> str:
    return "UTC"


@pytest.fixture
def auth(settings_service, clock) -> DashboardAuth:
    return DashboardAuth(settings_service, clock=clock)


class _Directory:
    """A Home Assistant panel directory double: one kitchen panel; records
    the connect requests (and so the claims) it is asked to deliver."""

    def __init__(self) -> None:
        self.panels = [
            ExternalDisplayPanel(
                id="dev-kitchen",
                name="Kitchen panel",
                mac=KITCHEN_MAC,
                connect_action="kitchen_connect_social_home",
            ),
            ExternalDisplayPanel(
                id="dev-hall",
                name="Hall",
                mac=HALL_MAC,
                connect_action="hall_connect_social_home",
            ),
        ]
        self.claims: dict[str, str] = {}

    async def list_panels(self) -> list[ExternalDisplayPanel]:
        return list(self.panels)

    async def connect(self, panel, *, url: str, claim: str) -> None:
        self.claims[panel.id] = claim


KITCHEN_MAC = "a0:b7:65:12:34:56"
HALL_MAC = "a0:b7:65:00:00:02"
CLAIM = "/api/display/v1/pair/claim"


@pytest.fixture
def directory() -> _Directory:
    return _Directory()


@pytest.fixture
def displays(settings_service, auth, clock, directory) -> DashboardDisplayService:
    return DashboardDisplayService(
        settings_service, auth, directory=directory, clock=clock
    )


async def _claim_for(displays, directory, panel_id: str = "dev-kitchen") -> str:
    await displays.connect_platform_panel(
        actor_is_admin=True, panel_id=panel_id, url="http://127.0.0.2:8100"
    )
    return directory.claims[panel_id]


@pytest.fixture
def theme_box() -> dict:
    """``theme_box["fn"]`` is the household theme the app reads."""
    return {"fn": default_theme}


@pytest.fixture
def tiles() -> PictureTiles:
    return PictureTiles()


@pytest.fixture
async def client(
    aiohttp_client, settings_service, auth, displays, snapshots, theme_box, tiles
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
        tiles=tiles,
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


async def _locked(client, displays, settings_service) -> tuple[str, str]:
    """A paired panel, locked over the API (a PIN is set first)."""
    await settings_service.set_pin(actor_is_admin=True, pin=PIN)
    display_id, token = await _paired(client, displays)
    resp = await client.post(LOCK, headers=_bearer(token))
    assert resp.status == 200, await resp.text()
    return display_id, token


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


# ── Pairing a panel connected from Home Assistant (claims) ──────────────


def _mac(mac: str) -> dict[str, str]:
    return {"X-Display-Mac": mac}


async def test_claim_pairs_the_panel(client, displays, directory, settings_service):
    claim = await _claim_for(displays, directory)
    resp = await client.post(
        CLAIM,
        json={"claim": claim, "model": "guition-jc8012p4a1", "firmware": "2026.10.13"},
        headers=_mac(KITCHEN_MAC.upper()),
    )
    assert resp.status == 200, await resp.text()
    assert resp.headers["Cache-Control"] == "no-store"
    body = await resp.json()
    _validate("pair-claim", body)
    _validate("pair-claim", json.loads((FIXTURE_DIR / "pair-claim.json").read_text()))
    (display,) = await settings_service.displays()
    assert body["display_id"] == display.id
    assert (display.name, display.mac, display.model) == (
        "Kitchen panel",
        KITCHEN_MAC,
        "guition-jc8012p4a1",
    )
    # The token works on the API at once.
    who = await client.get(WHOAMI, headers=_bearer(body["token"]))
    assert await who.json() == {"display_id": display.id, "name": "Kitchen panel"}


async def _refused(resp) -> None:
    assert resp.status == 404
    assert await resp.json() == {
        "error": {"code": "NOT_FOUND", "detail": "No such pairing."}
    }


async def test_claim_replay_is_404(client, displays, directory):
    claim = await _claim_for(displays, directory)
    ok = await client.post(CLAIM, json={"claim": claim}, headers=_mac(KITCHEN_MAC))
    assert ok.status == 200
    await _refused(
        await client.post(CLAIM, json={"claim": claim}, headers=_mac(KITCHEN_MAC))
    )


async def test_claim_for_another_panel_is_404_and_burnt(client, displays, directory):
    claim = await _claim_for(displays, directory)
    await _refused(
        await client.post(CLAIM, json={"claim": claim}, headers=_mac(HALL_MAC))
    )
    # The panel it was minted for can't use it after that.
    await _refused(
        await client.post(CLAIM, json={"claim": claim}, headers=_mac(KITCHEN_MAC))
    )


@pytest.mark.parametrize(
    "body", [{}, {"claim": None}, {"claim": "A" * 43}, {"claim": "short"}]
)
async def test_wrong_claims_are_a_uniform_404(client, displays, directory, body):
    await _claim_for(displays, directory)
    await _refused(await client.post(CLAIM, json=body, headers=_mac(KITCHEN_MAC)))


async def test_expired_claim_is_404(client, displays, directory, clock):
    claim = await _claim_for(displays, directory)
    clock.advance(CLAIM_TTL_SECONDS + 1)
    await _refused(
        await client.post(CLAIM, json={"claim": claim}, headers=_mac(KITCHEN_MAC))
    )


async def test_claim_needs_no_token_and_is_not_the_poll_route(client):
    # Public (one segment below /pair) and routed to the claim view: a GET
    # is 405, not the pair-status 404.
    assert is_public_display_path(CLAIM)
    assert (await client.get(CLAIM)).status == 405


@pytest.mark.parametrize("body", [b"{nope", b"[1]"])
async def test_claim_bad_json_is_400(client, body):
    resp = await client.post(
        CLAIM, data=body, headers={"Content-Type": "application/json"}
    )
    assert resp.status == 400


async def test_claim_shares_the_pair_throttle(client):
    for _ in range(PAIR_MAX):
        await client.post(CLAIM, json={"claim": "A" * 43})
    resp = await client.post(CLAIM, json={"claim": "A" * 43})
    assert resp.status == 429 and int(resp.headers["Retry-After"]) > 0
    assert (await client.post(PAIR)).status == 429


async def test_claim_refused_while_disabled(
    client, displays, directory, settings_service
):
    claim = await _claim_for(displays, directory)
    await settings_service.update(actor_is_admin=True, patch={"enabled": False})
    resp = await client.post(CLAIM, json={"claim": claim}, headers=_mac(KITCHEN_MAC))
    assert resp.status == 409
    assert (await resp.json())["error"]["code"] == "DASHBOARD_DISABLED"


async def test_panels_report_their_mac(client, displays, settings_service, clock):
    """``X-Display-Mac`` on /pair is kept at pairing; on any later request
    it is recorded like the firmware / model headers."""
    resp = await client.post(PAIR, headers=_mac("A0-B7-65-12-34-56"))
    started = await resp.json()
    await displays.claim(actor_is_admin=True, code=started["code"], name="K")
    (display,) = await settings_service.displays()
    assert display.mac == KITCHEN_MAC
    _, token = await _paired(client, displays, name="Old firmware")
    clock.advance(SEEN_INTERVAL_S + 1)
    await client.get(WHOAMI, headers={**_bearer(token), **_mac("a0b765000002")})
    assert [d.mac for d in await settings_service.displays()] == [
        KITCHEN_MAC,
        HALL_MAC,
    ]


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
    "/api/display/v1/map.png",
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
    client, displays, snapshots, settings_service, path
):
    _seed(snapshots)
    _, token = await _locked(client, displays, settings_service)
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


async def test_locked_display_is_rate_limited_too(
    client, displays, auth, settings_service
):
    display_id, token = await _locked(client, displays, settings_service)
    for _ in range(DISPLAY_READ_MAX):
        auth.begin_display_read(display_id)
    assert (await _get(client, "/api/display/v1/home", token)).status == 429


async def test_night_answers_409_even_for_a_tab_that_is_off(
    client, displays, snapshots
):
    """At night a tab never says whether it would show by day."""
    _, token = await _paired(client, displays)
    snapshots.home_snapshot = home(is_night=True, sections=frozenset({"calendar"}))
    for tab in ("messages", "notes", "map"):
        assert (await _get(client, f"/api/display/v1/{tab}", token)).status == 409


# ── Lock / unlock / Got it ───────────────────────────────────────────────


async def test_lock_needs_a_pin(client, displays):
    _, token = await _paired(client, displays)
    resp = await client.post(LOCK, headers=_bearer(token))
    assert resp.status == 409
    body = await resp.json()
    _validate("error", body)
    assert body["error"]["code"] == "DASHBOARD_PIN_REQUIRED"
    assert (await client.get(WHOAMI, headers=_bearer(token))).status == 200


async def test_lock_answers_the_lock_screen_and_locks_every_read(
    client, displays, snapshots, settings_service
):
    _seed(snapshots)
    await settings_service.set_pin(actor_is_admin=True, pin=PIN)
    _, token = await _paired(client, displays)
    resp = await client.post(LOCK, headers=_bearer(token))
    assert resp.status == 200
    payload = await resp.json()
    _validate("lock", payload)
    for path in [*BOARD_PATHS, WHOAMI, ACK]:
        method = client.post if path == ACK else client.get
        assert (await method(path, headers=_bearer(token))).status == 423, path
    # Locking a locked panel is the lock screen too (no new state).
    assert (await client.post(LOCK, headers=_bearer(token))).status == 423


async def test_lock_and_unlock_need_a_token(client):
    for path in (LOCK, UNLOCK, ACK):
        resp = await client.post(path, json={"pin": PIN})
        assert resp.status == 401
        assert resp.headers["WWW-Authenticate"] == "Bearer"


async def test_tablet_cookie_cannot_lock_or_unlock_a_panel(client, auth):
    cookie = {"Cookie": f"{COOKIE_NAME}={await auth.issue(True)}"}
    for path in (LOCK, UNLOCK, ACK):
        assert (await client.post(path, headers=cookie)).status == 401


async def test_unlock_with_the_pin(client, displays, settings_service):
    _, token = await _locked(client, displays, settings_service)
    resp = await client.post(UNLOCK, json={"pin": PIN}, headers=_bearer(token))
    assert resp.status == 200
    body = await resp.json()
    _validate("unlock", body)
    assert body == {"api_version": 1, "locked": False}
    _fixture("unlocked", body)
    assert (await client.get(WHOAMI, headers=_bearer(token))).status == 200


async def test_unlock_an_unlocked_panel_is_a_no_op(client, displays, settings_service):
    await settings_service.set_pin(actor_is_admin=True, pin=PIN)
    _, token = await _paired(client, displays)
    resp = await client.post(UNLOCK, json={"pin": "0000"}, headers=_bearer(token))
    assert resp.status == 200
    assert (await resp.json())["locked"] is False


@pytest.mark.parametrize("body", [b"{not json", b"[]", b'"1234"'])
async def test_unlock_bad_body_is_400(client, displays, settings_service, body):
    _, token = await _locked(client, displays, settings_service)
    resp = await client.post(
        UNLOCK,
        data=body,
        headers={**_bearer(token), "Content-Type": "application/json"},
    )
    assert resp.status == 400
    _validate("error", await resp.json())


async def test_wrong_pins_then_the_panel_is_signed_out(
    client, displays, settings_service
):
    display_id, token = await _locked(client, displays, settings_service)
    for left in range(THROTTLE_MAX_FAILURES - 1, -1, -1):
        resp = await client.post(UNLOCK, json={"pin": "0000"}, headers=_bearer(token))
        assert resp.status == 403
        body = await resp.json()
        _validate("error", body)
        assert body["error"]["code"] == "DISPLAY_PIN_WRONG"
        assert body["error"]["params"] == {"remaining": left}
        if left == THROTTLE_MAX_FAILURES - 1:
            _fixture("pin-wrong", body)
    # The sixth try — even the right PIN — unpairs the panel.
    resp = await client.post(UNLOCK, json={"pin": PIN}, headers=_bearer(token))
    assert resp.status == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"
    body = await resp.json()
    _validate("error", body)
    assert body["error"]["code"] == "DISPLAY_SIGNED_OUT"
    assert (await client.get(WHOAMI, headers=_bearer(token))).status == 401
    assert display_id not in {d.id for d in await settings_service.displays()}


async def test_a_wrong_pin_type_counts_as_wrong(client, displays, settings_service):
    _, token = await _locked(client, displays, settings_service)
    resp = await client.post(UNLOCK, json={"pin": 2468}, headers=_bearer(token))
    assert resp.status == 403


async def test_lock_survives_a_restart(
    aiohttp_client, client, displays, settings_service, snapshots, clock
):
    _, token = await _locked(client, displays, settings_service)
    # A fresh process over the same storage: services and app rebuilt.
    fresh = DashboardSettingsService(
        settings_service._repo,  # type: ignore[attr-defined]
        fixed_port=None,
        main_port=8080,
    )
    fresh.attach_key_manager(settings_service._key_manager)  # type: ignore[attr-defined]
    auth = DashboardAuth(fresh, clock=clock)
    app = build_dashboard_app(
        settings_service=fresh,
        auth=auth,
        displays=DashboardDisplayService(fresh, auth, clock=clock),
        renderer=RENDERER,
        household_name=_name,
        household_tz=_tz,
        household_theme=default_theme,
        snapshots=snapshots,
    )
    again = await aiohttp_client(app, cookie_jar=aiohttp.DummyCookieJar())
    assert (await again.get(WHOAMI, headers=_bearer(token))).status == 423


async def test_removing_the_pin_lifts_every_panel_lock(
    client, displays, settings_service
):
    _, token = await _locked(client, displays, settings_service)
    await settings_service.delete_pin(actor_is_admin=True)
    assert (await client.get(WHOAMI, headers=_bearer(token))).status == 200
    # A new PIN doesn't lock it again.
    await settings_service.set_pin(actor_is_admin=True, pin=PIN)
    assert (await client.get(WHOAMI, headers=_bearer(token))).status == 200


async def test_ack(client, displays, snapshots):
    _seed(snapshots)
    snapshots.ackable = {("h1", None), ("m2", "u-leo")}
    _, token = await _paired(client, displays)
    resp = await client.post(ACK, json={"message_id": "h1"}, headers=_bearer(token))
    assert resp.status == 200
    body = await resp.json()
    _validate("ack", body)
    assert body == {"api_version": 1, "acknowledged": True}
    _fixture("ack", body)
    resp = await client.post(
        ACK,
        json={"message_id": "m2", "person_id": "u-leo"},
        headers=_bearer(token),
    )
    assert resp.status == 200
    assert snapshots.acks == [("h1", None), ("m2", "u-leo")]


@pytest.mark.parametrize(
    "body",
    [
        {"message_id": "m2"},  # a person's mention is no @here
        {"message_id": "h1", "person_id": "u-anna"},  # not that person's
        {"message_id": "nope"},  # no such message
        {"message_id": "m2", "person_id": "u-hidden"},  # not shown
        {"message_id": ""},
        {"message_id": "x" * 129},
        {"message_id": 7},
        {"message_id": "h1", "person_id": 7},
        {"message_id": "h1", "person_id": ""},
        {},
    ],
)
async def test_ack_of_anything_the_wall_does_not_show_is_404(
    client, displays, snapshots, body
):
    _seed(snapshots)
    snapshots.ackable = {("h1", None), ("m2", "u-leo")}
    _, token = await _paired(client, displays)
    resp = await client.post(ACK, json=body, headers=_bearer(token))
    assert resp.status == 404
    payload = await resp.json()
    _validate("error", payload)
    assert payload["error"]["code"] == "NOT_FOUND"
    assert snapshots.acks == []


async def test_ack_bad_json_is_400(client, displays):
    _, token = await _paired(client, displays)
    resp = await client.post(
        ACK,
        data=b"[1]",
        headers={**_bearer(token), "Content-Type": "application/json"},
    )
    assert resp.status == 400


async def test_ack_is_capped_per_display(client, displays, snapshots, auth):
    snapshots.ackable = {("h1", None)}
    display_id, token = await _paired(client, displays)
    for _ in range(ACK_MAX):
        assert auth.begin_display_ack(display_id) == 0
    resp = await client.post(ACK, json={"message_id": "h1"}, headers=_bearer(token))
    assert resp.status == 429
    assert int(resp.headers["Retry-After"]) > 0
    assert snapshots.acks == []
    # Another panel has its own budget.
    _, other = await _paired(client, displays, name="Hall")
    resp = await client.post(ACK, json={"message_id": "h1"}, headers=_bearer(other))
    assert resp.status == 200


async def test_ack_refused_while_locked(client, displays, snapshots, settings_service):
    snapshots.ackable = {("h1", None)}
    _, token = await _locked(client, displays, settings_service)
    resp = await client.post(ACK, json={"message_id": "h1"}, headers=_bearer(token))
    assert resp.status == 423
    assert snapshots.acks == []


# ── The map as a picture ─────────────────────────────────────────────────

MAP_PNG = "/api/display/v1/map.png"


async def _map_png(client, token, query: str = "", **kw):
    return await _get(client, MAP_PNG + query, token, **kw)


def _size(body: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(body)) as img:
        assert img.format == "PNG"
        return img.size


async def test_map_png_is_the_map_json_image(client, displays, snapshots, tiles):
    _seed(snapshots)
    snapshots.pictures["u-anna"] = (solid_png((255, 0, 0), 96), "pic1")
    _, token = await _paired(client, displays)
    image = (await (await _get(client, "/api/display/v1/map", token)).json())["map"][
        "image"
    ]
    assert image["url"] == MAP_PNG and (image["width"], image["height"]) == (748, 560)
    resp = await _map_png(client, token)
    assert resp.status == 200 and resp.content_type == "image/png"
    assert resp.headers["Cache-Control"] == "private, no-cache"
    assert resp.headers["ETag"] == image["etag"]
    assert _size(await resp.read()) == (748, 560)
    fetched = len(tiles.calls)
    assert fetched > 0
    # Polling an unchanged map: a 304, nothing painted, no tile asked for.
    again = await _map_png(client, token, headers={"If-None-Match": image["etag"]})
    assert again.status == 304 and await again.read() == b""
    assert again.headers["ETag"] == image["etag"]
    # Another size or scheme is another picture, with its own ETag — a
    # panel keeping one tag across a change never gets a wrong 304.
    other = await _map_png(
        client,
        token,
        "?w=1280&h=800&scheme=dark",
        headers={"If-None-Match": image["etag"]},
    )
    assert other.status == 200 and other.headers["ETag"] != image["etag"]
    assert _size(await other.read()) == (1280, 800)
    assert len(tiles.calls) > fetched
    dark = await _map_png(client, token, "?scheme=dark")
    assert dark.headers["ETag"] not in (image["etag"], other.headers["ETag"])


async def test_map_png_etag_follows_the_people(client, displays, snapshots):
    _seed(snapshots)
    _, token = await _paired(client, displays)
    first = (await _map_png(client, token)).headers["ETag"]
    moved = list(MAP_PEOPLE)
    moved[1] = replace(moved[1], latitude=52.5240)
    snapshots.home_snapshot = map_home(people=tuple(moved))
    resp = await _map_png(client, token, headers={"If-None-Match": first})
    assert resp.status == 200 and resp.headers["ETag"] != first


@pytest.mark.parametrize(
    ("query", "size"),
    [
        ("?w=10&h=99999", (200, 1200)),
        ("?w=4000&h=1", (1920, 200)),
        ("?w=640", (640, 560)),
    ],
)
async def test_map_png_size_is_held_to_bounds(client, displays, snapshots, query, size):
    _seed(snapshots)
    _, token = await _paired(client, displays)
    resp = await _map_png(client, token, query)
    assert resp.status == 200
    assert _size(await resp.read()) == size


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("?w=abc", "BAD_SIZE"),
        ("?h=-5", "BAD_SIZE"),
        ("?w=123456", "BAD_SIZE"),
        ("?w=%EF%BC%95", "BAD_SIZE"),  # a full-width digit
        ("?scheme=night", "BAD_SCHEME"),
    ],
)
async def test_map_png_bad_query_is_400(client, displays, snapshots, query, code):
    _seed(snapshots)
    _, token = await _paired(client, displays)
    resp = await _map_png(client, token, query)
    assert resp.status == 400
    body = await resp.json()
    _validate("error", body)
    assert body["error"]["code"] == code


async def test_map_png_follows_the_map_tab_rules(client, displays, snapshots, tiles):
    _, token = await _paired(client, displays)
    # Nobody located (or presence off): the tab doesn't show.
    snapshots.home_snapshot = home()
    resp = await _map_png(client, token)
    assert resp.status == 404 and (await resp.json())["error"]["code"] == "TAB_OFF"
    snapshots.home_snapshot = map_home(sections=frozenset({"calendar"}))
    assert (await _map_png(client, token)).status == 404
    # Night: back to Overview.
    snapshots.home_snapshot = map_home(is_night=True)
    resp = await _map_png(client, token)
    assert resp.status == 409 and (await resp.json())["error"]["code"] == "NIGHT"
    assert tiles.calls == []  # nothing was drawn for any of them


async def test_map_png_reads_only_shown_faces(client, displays, snapshots):
    """Hidden people are not in the read model, so the picture never asks
    for them; a shown person's picture is read through the same rule as
    /avatar (the read model's ``avatar``)."""
    _seed(snapshots)
    snapshots.pictures["u-anna"] = (solid_png((255, 0, 0), 96), "anna-pic")
    snapshots.pictures["u-hidden"] = (solid_png((0, 255, 0), 96), "hidden-pic")
    _, token = await _paired(client, displays)
    snapshots.calls.clear()
    assert (await _map_png(client, token)).status == 200
    assert "avatar:u-anna" in snapshots.calls
    assert "avatar:u-hidden" not in snapshots.calls


async def test_map_png_with_some_tiles_down_still_draws(
    client, displays, snapshots, tiles
):
    _seed(snapshots)
    _, token = await _paired(client, displays)
    image = (await (await _get(client, "/api/display/v1/map", token)).json())["map"][
        "image"
    ]
    first = await _map_png(client, token, "?w=400&h=300")
    assert first.status == 200
    down = tiles.calls[0]
    tiles.fail = {down}
    resp = await _map_png(client, token, "?w=401&h=300")
    assert resp.status == 200
    # A picture with a gap has an ETag of its own: once the tiles are back
    # the whole picture replaces it.
    assert resp.headers["ETag"] != image["etag"]


async def test_map_png_with_no_tiles_is_503(client, displays, snapshots, tiles):
    _seed(snapshots)
    tiles.fail = None
    _, token = await _paired(client, displays)
    resp = await _map_png(client, token)
    assert resp.status == 503
    assert int(resp.headers["Retry-After"]) > 0
    body = await resp.json()
    _validate("error", body)
    assert body["error"]["code"] == "TILES_UNAVAILABLE"


async def test_map_png_without_a_tile_source(
    aiohttp_client, settings_service, auth, displays, snapshots
):
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
        snapshots=snapshots,
    )
    bare = await aiohttp_client(app, cookie_jar=aiohttp.DummyCookieJar())
    _seed(snapshots)
    _, token = await _paired(bare, displays)
    payload = await (await _get(bare, "/api/display/v1/map", token)).json()
    _validate("map", payload)
    assert payload["map"]["image"] is None
    assert (await _map_png(bare, token)).status == 503


async def test_map_png_counts_as_an_image_and_painting_is_throttled(
    client, displays, snapshots, auth, monkeypatch
):
    _seed(snapshots)
    display_id, token = await _paired(client, displays)
    kinds: list[bool] = []
    real = DashboardAuth.begin_display_read

    def spy(self, display: str, *, image: bool = False) -> int:
        kinds.append(image)
        return real(self, display, image=image)

    monkeypatch.setattr(DashboardAuth, "begin_display_read", spy)
    assert (await _map_png(client, token, "?w=300&h=300")).status == 200
    assert kinds == [True]
    # Each new size is a new painting: the budget runs out…
    for k in range(1, MAP_RENDER_MAX):
        assert (await _map_png(client, token, f"?w={300 + k}&h=300")).status == 200
    resp = await _map_png(client, token, "?w=999&h=300")
    assert resp.status == 429 and int(resp.headers["Retry-After"]) > 0
    # …but a kept picture is still handed out.
    kept = await _map_png(client, token, "?w=300&h=300")
    assert kept.status == 200
    # Another panel paints its own.
    _, other = await _paired(client, displays, name="Hall")
    assert (await _map_png(client, other, "?w=999&h=300")).status == 200


async def test_locked_panel_gets_no_map_png(
    client, displays, snapshots, tiles, settings_service
):
    _seed(snapshots)
    _, token = await _locked(client, displays, settings_service)
    snapshots.calls.clear()
    resp = await _map_png(client, token)
    assert resp.status == 423 and resp.content_type == "application/json"
    assert tiles.calls == [] and snapshots.calls == []
