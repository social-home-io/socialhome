"""Integration tests for the household-dashboard admin API.

``/api/admin/dashboard*`` — settings, password, tablet setup link and
"sign out all tablets". The dashboard itself listens on its own port;
these tests bind ephemeral ports and the app's cleanup stops it.
"""

from __future__ import annotations

import socket
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest

from socialhome.app_keys import (
    dashboard_auth_key,
    dashboard_server_key,
    dashboard_settings_service_key,
    platform_adapter_key,
)
from socialhome.dashboard.auth import COOKIE_NAME
from socialhome.platform.adapter import DashboardHostPort
from socialhome.routes.admin_dashboard import dashboard_base_url

from .conftest import _auth

PASSWORD = "correct horse"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
async def _stop_dashboard(client):
    yield
    await client.app[dashboard_server_key].stop()


async def _demote(client) -> None:
    await client._db.enqueue(
        "UPDATE users SET is_admin=0 WHERE user_id=?", (client._uid,)
    )


async def _set_password(client) -> None:
    resp = await client.put(
        "/api/admin/dashboard/password",
        headers=_auth(client._tok),
        json={"password": PASSWORD},
    )
    assert resp.status == 204


async def _put(client, patch: dict):
    return await client.put(
        "/api/admin/dashboard", headers=_auth(client._tok), json=patch
    )


async def _get_status(port: int, path: str = "/healthz") -> int:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{port}{path}") as resp:
            return resp.status


# ── Pure helper ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("address", "host", "port", "expected"),
    [
        ("https://dash.example.com", "ignored:1", 8100, "https://dash.example.com"),
        (None, "homeassistant.local:8123", 8100, "http://homeassistant.local:8100"),
        (None, "homeassistant.local", 8100, "http://homeassistant.local:8100"),
        (None, "192.168.1.5:8099", 9000, "http://192.168.1.5:9000"),
        (None, "[fd00::1]:8099", 8100, "http://[fd00::1]:8100"),
        (None, "[fd00::1]", 8100, "http://[fd00::1]:8100"),
        (None, "Box.LAN:80", 8100, "http://box.lan:8100"),
        (None, "", 8100, "http://localhost:8100"),
        (None, "[not-an-ip]:80", 8100, "http://localhost:8100"),
        (None, "[fd00::1", 8100, "http://localhost:8100"),
        (None, None, 8100, "http://localhost:8100"),
    ],
)
def test_dashboard_base_url(address, host, port, expected):
    assert dashboard_base_url(address, host, port) == expected


# ── Access control ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/admin/dashboard", None),
        ("PUT", "/api/admin/dashboard", {"locale": "de"}),
        ("PUT", "/api/admin/dashboard/password", {"password": PASSWORD}),
        ("POST", "/api/admin/dashboard/setup-link", None),
        ("DELETE", "/api/admin/dashboard/sessions", None),
        ("PUT", "/api/admin/dashboard/pin", {"pin": "1234"}),
        ("DELETE", "/api/admin/dashboard/pin", None),
        ("GET", "/api/admin/dashboard/displays", None),
        ("POST", "/api/admin/dashboard/displays", {"code": "ABCDEF", "name": "K"}),
        ("PATCH", "/api/admin/dashboard/displays/d1", {"name": "K"}),
        ("PATCH", "/api/admin/dashboard/displays/d1", {"locked": False}),
        ("DELETE", "/api/admin/dashboard/displays/d1", None),
    ],
)
async def test_non_admin_is_refused(client, method, path, body):
    await _demote(client)
    resp = await client.request(method, path, headers=_auth(client._tok), json=body)
    assert resp.status == 403


async def test_unauthenticated_is_refused(client):
    resp = await client.get("/api/admin/dashboard")
    assert resp.status == 401


# ── GET ─────────────────────────────────────────────────────────────────


async def test_get_defaults(client):
    resp = await client.get("/api/admin/dashboard", headers=_auth(client._tok))
    assert resp.status == 200
    body = await resp.json()
    assert body["enabled"] is False
    assert body["running"] is False
    assert body["running_port"] is None
    assert body["port"] == 8100
    assert body["port_fixed"] is False
    assert body["password_set"] is False
    assert body["password_set_at"] is None
    assert body["pin_set"] is False
    assert body["address"] is None
    host = urlsplit(f"http://{client.host}").hostname
    assert body["url"] == f"http://{host}:8100"
    assert body["locale"] == "en"
    assert body["idle_return_minutes"] == 3
    assert body["night_start"] == "22:00"
    assert body["night_end"] == "06:00"
    assert body["keep_screen_on"] is True
    assert body["show_status_text"] is False
    assert body["show_household_messages"] is False
    assert body["mention_sound"] is True
    assert body["household_chat_enabled"] is True
    assert body["url_host_allowed"] is True
    assert body["host_port_mapped"] is None
    assert body["last_seen_at"] is None
    assert isinstance(body["people"], list)


async def test_get_url_follows_request_host(client):
    resp = await client.get(
        "/api/admin/dashboard",
        headers={**_auth(client._tok), "Host": "homeassistant.local:8123"},
    )
    assert (await resp.json())["url"] == "http://homeassistant.local:8100"


async def test_get_url_under_ha_ingress(client):
    """HA Core's ingress proxy sets ``X-Forwarded-Host`` to the host the
    browser used; the App sees the Supervisor hop as the peer."""
    resp = await client.get(
        "/api/admin/dashboard",
        headers={
            **_auth(client._tok),
            "Host": "172.30.33.5:8099",
            "X-Forwarded-Host": "homeassistant.local:8123",
        },
    )
    body = await resp.json()
    assert body["url"] == "http://homeassistant.local:8100"
    assert body["url_host_allowed"] is True


async def test_get_url_uses_the_first_forwarded_host(client):
    resp = await client.get(
        "/api/admin/dashboard",
        headers={
            **_auth(client._tok),
            "X-Forwarded-Host": "box.lan:443, proxy.internal",
        },
    )
    assert (await resp.json())["url"] == "http://box.lan:8100"


async def test_url_host_allowed_flags_a_public_name(client):
    resp = await client.get(
        "/api/admin/dashboard",
        headers={**_auth(client._tok), "Host": "social.example.com"},
    )
    body = await resp.json()
    assert body["url"] == "http://social.example.com:8100"
    assert body["url_host_allowed"] is False
    resp = await _put(client, {"address": "https://social.example.com"})
    body = await resp.json()
    assert body["url"] == "https://social.example.com"
    assert body["url_host_allowed"] is True


# ── Platform-published host port (Home Assistant App) ──────────────────


def _host_port(monkeypatch, client, result: DashboardHostPort | None) -> None:
    """Make the platform adapter report ``result`` as the dashboard's
    published host port (the Home Assistant App asks Supervisor)."""

    async def fake(self):
        return result

    adapter = client.app[platform_adapter_key]
    monkeypatch.setattr(type(adapter), "dashboard_host_port", fake)


async def test_get_url_uses_the_mapped_host_port(client, monkeypatch):
    _host_port(monkeypatch, client, DashboardHostPort(18100))
    resp = await client.get(
        "/api/admin/dashboard",
        headers={**_auth(client._tok), "X-Forwarded-Host": "homeassistant.local:8123"},
    )
    body = await resp.json()
    assert body["url"] == "http://homeassistant.local:18100"
    assert body["port"] == 8100
    assert body["host_port_mapped"] is True


async def test_get_unmapped_host_port_tells_the_admin(client, monkeypatch):
    _host_port(monkeypatch, client, DashboardHostPort(None))
    resp = await client.get(
        "/api/admin/dashboard",
        headers={**_auth(client._tok), "X-Forwarded-Host": "homeassistant.local:8123"},
    )
    body = await resp.json()
    assert body["url"] == "http://homeassistant.local:8100"
    assert body["host_port_mapped"] is False


async def test_get_undeclared_host_port_is_null(client, monkeypatch):
    """The App doesn't declare the port (older App releases): nothing the
    admin can do, so no "turn it on" prompt — the container port stays."""
    _host_port(monkeypatch, client, DashboardHostPort(None, declared=False))
    resp = await client.get(
        "/api/admin/dashboard",
        headers={**_auth(client._tok), "Host": "homeassistant.local:8123"},
    )
    body = await resp.json()
    assert body["url"] == "http://homeassistant.local:8100"
    assert body["host_port_mapped"] is None


async def test_put_reports_the_mapped_host_port(client, monkeypatch):
    _host_port(monkeypatch, client, DashboardHostPort(9200))
    body = await (await _put(client, {"locale": "de"})).json()
    assert body["url"].endswith(":9200")
    assert body["host_port_mapped"] is True


async def test_address_wins_over_the_mapped_host_port(client, monkeypatch):
    _host_port(monkeypatch, client, DashboardHostPort(9200))
    body = await (await _put(client, {"address": "https://dash.example.com"})).json()
    assert body["url"] == "https://dash.example.com"
    assert body["host_port_mapped"] is True


async def test_get_last_seen_at_is_iso_utc(client):
    client.app[dashboard_auth_key].last_seen_at = 0.0
    resp = await client.get("/api/admin/dashboard", headers=_auth(client._tok))
    assert (await resp.json())["last_seen_at"] == "1970-01-01T00:00:00+00:00"


async def test_people_list(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_minor, "
        "child_protection_enabled, declared_age, picture_hash) "
        "VALUES(?,?,?,?,?,?,?)",
        ("kid", "uid-kid", "Kid", 1, 1, 9, "abc123abc123abc1"),
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("grown", "uid-grown", "Grown"),
    )
    resp = await _put(
        client, {"people": {"uid-grown": {"show": False, "picture": True}}}
    )
    assert resp.status == 200
    people = {p["user_id"]: p for p in (await resp.json())["people"]}

    kid = people["uid-kid"]
    assert kid["username"] == "kid"
    assert kid["display_name"] == "Kid"
    assert kid["show"] is True
    assert kid["picture"] is None
    assert kid["picture_effective"] is True
    assert kid["picture_url"].startswith("api/users/uid-kid/picture?v=abc123")
    assert "sig=" in kid["picture_url"]

    grown = people["uid-grown"]
    assert grown["show"] is False
    assert grown["picture"] is True
    assert grown["picture_effective"] is True
    assert grown["picture_url"] is None

    admin = people[client._uid]
    assert admin["picture"] is None
    assert admin["picture_effective"] is False

    for entry in people.values():
        assert set(entry) == {
            "user_id",
            "username",
            "display_name",
            "picture_url",
            "show",
            "picture",
            "picture_effective",
        }
        assert "is_minor" not in entry
        assert "declared_age" not in entry


# ── PUT settings ────────────────────────────────────────────────────────


async def test_enable_without_password_is_refused(client):
    resp = await _put(client, {"enabled": True, "port": _free_port()})
    assert resp.status == 422
    assert (await resp.json())["error"]["code"] == "DASHBOARD_PASSWORD_REQUIRED"


async def test_invalid_address_is_refused(client):
    resp = await _put(client, {"address": "ftp://dash.local"})
    assert resp.status == 422
    body = await resp.json()
    assert body["error"]["code"] == "DASHBOARD_SETTINGS_INVALID"
    assert body["error"]["params"] == {"field": "address"}


async def test_address_is_used_as_url(client):
    resp = await _put(client, {"address": "https://dash.example.com/"})
    assert resp.status == 200
    body = await resp.json()
    assert body["address"] == "https://dash.example.com"
    assert body["url"] == "https://dash.example.com"


async def test_household_messages_toggle_round_trips(client):
    resp = await _put(client, {"show_household_messages": True})
    assert resp.status == 200
    assert (await resp.json())["show_household_messages"] is True
    resp = await client.get("/api/admin/dashboard", headers=_auth(client._tok))
    assert (await resp.json())["show_household_messages"] is True


async def test_mention_sound_toggle_round_trips(client):
    resp = await _put(client, {"mention_sound": False})
    assert resp.status == 200
    assert (await resp.json())["mention_sound"] is False
    resp = await client.get("/api/admin/dashboard", headers=_auth(client._tok))
    assert (await resp.json())["mention_sound"] is False


async def test_mention_sound_toggle_must_be_a_bool(client):
    resp = await _put(client, {"mention_sound": "no"})
    assert resp.status == 422
    body = await resp.json()
    assert body["error"]["params"] == {"field": "mention_sound"}


async def test_household_messages_toggle_must_be_a_bool(client):
    resp = await _put(client, {"show_household_messages": "yes"})
    assert resp.status == 422
    body = await resp.json()
    assert body["error"]["params"] == {"field": "show_household_messages"}


async def test_household_chat_enabled_follows_the_household_toggle(client):
    resp = await client.put(
        "/api/household/preferences",
        json={"toggles": {"feat_household_chat": False}},
        headers=_auth(client._tok),
    )
    assert resp.status == 200
    resp = await client.get("/api/admin/dashboard", headers=_auth(client._tok))
    assert (await resp.json())["household_chat_enabled"] is False


async def test_enable_on_a_free_port_starts_the_listener(client):
    await _set_password(client)
    port = _free_port()
    resp = await _put(client, {"enabled": True, "port": port})
    assert resp.status == 200
    body = await resp.json()
    assert body["enabled"] is True
    assert body["running"] is True
    assert body["running_port"] == port
    assert body["password_set"] is True
    assert await _get_status(port) == 200

    resp = await _put(client, {"enabled": False})
    body = await resp.json()
    assert body["running"] is False
    with pytest.raises(aiohttp.ClientConnectionError):
        await _get_status(port)


async def test_port_in_use_is_409_and_settings_unchanged(client):
    await _set_password(client)
    first = _free_port()
    assert (await _put(client, {"enabled": True, "port": first})).status == 200
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("0.0.0.0", 0))
        blocker.listen()
        taken = blocker.getsockname()[1]
        resp = await _put(client, {"port": taken, "locale": "de"})
        assert resp.status == 409
        body = await resp.json()
        assert body["error"]["code"] == "DASHBOARD_PORT_IN_USE"
        assert body["error"]["params"] == {"port": taken}
    stored = await client.app[dashboard_settings_service_key].get()
    assert stored.enabled is True
    assert stored.port == first
    assert stored.locale == "en"
    assert client.app[dashboard_server_key].running_port == first
    assert await _get_status(first) == 200


async def test_enable_on_a_taken_port_stays_disabled(client):
    await _set_password(client)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("0.0.0.0", 0))
        blocker.listen()
        taken = blocker.getsockname()[1]
        resp = await _put(client, {"enabled": True, "port": taken})
        assert resp.status == 409
    stored = await client.app[dashboard_settings_service_key].get()
    assert stored.enabled is False
    assert stored.port == 8100


async def test_other_edits_save_while_the_port_is_taken(client):
    """Enabled, but the port was taken at startup: ``running`` is false and
    edits that don't move the listener still save."""
    await _set_password(client)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("0.0.0.0", 0))
        blocker.listen()
        taken = blocker.getsockname()[1]
        svc = client.app[dashboard_settings_service_key]
        await svc.update(actor_is_admin=True, patch={"enabled": True, "port": taken})
        resp = await _put(client, {"locale": "de"})
        assert resp.status == 200
        body = await resp.json()
        assert body["locale"] == "de"
        assert body["enabled"] is True
        assert body["running"] is False
    assert (await svc.get()).locale == "de"


# ── Password ────────────────────────────────────────────────────────────


async def test_short_password_is_refused(client):
    resp = await client.put(
        "/api/admin/dashboard/password",
        headers=_auth(client._tok),
        json={"password": "short"},
    )
    assert resp.status == 422
    assert (await resp.json())["error"]["code"] == "DASHBOARD_PASSWORD_TOO_SHORT"


async def test_set_password_marks_it_set_and_signs_tablets_out(client):
    svc = client.app[dashboard_settings_service_key]
    before = await svc.epoch()
    await _set_password(client)
    assert await svc.epoch() == before + 1
    body = await (
        await client.get("/api/admin/dashboard", headers=_auth(client._tok))
    ).json()
    assert body["password_set"] is True
    assert body["password_set_at"].endswith("+00:00")
    assert await svc.verify_password(PASSWORD)


# ── Setup link + sessions ───────────────────────────────────────────────


async def test_setup_link_requires_a_running_dashboard(client):
    await _set_password(client)
    resp = await client.post(
        "/api/admin/dashboard/setup-link", headers=_auth(client._tok)
    )
    assert resp.status == 409
    body = await resp.json()
    assert body["error"]["code"] == "DASHBOARD_NOT_RUNNING"
    assert body["error"]["detail"] == (
        "Turn the dashboard on before creating a sign-in link."
    )


async def test_setup_link_signs_a_tablet_in(client):
    await _set_password(client)
    port = _free_port()
    assert (await _put(client, {"enabled": True, "port": port})).status == 200
    resp = await client.post(
        "/api/admin/dashboard/setup-link",
        headers={**_auth(client._tok), "Host": f"127.0.0.1:{client.port}"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["expires_at"].endswith("+00:00")
    parts = urlsplit(body["url"])
    assert f"{parts.scheme}://{parts.netloc}" == f"http://127.0.0.1:{port}"
    assert parts.path == "/enter"
    token = parse_qs(parts.query)["t"][0]

    origin = f"http://127.0.0.1:{port}"
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{origin}/enter",
            data={"t": token},
            headers={"Origin": origin},
            allow_redirects=False,
        ) as enter:
            assert enter.status in (302, 303)
            assert enter.headers["Location"] == "/"
            assert COOKIE_NAME in enter.cookies


async def test_setup_link_uses_the_mapped_host_port(client, monkeypatch):
    await _set_password(client)
    port = _free_port()
    assert (await _put(client, {"enabled": True, "port": port})).status == 200
    _host_port(monkeypatch, client, DashboardHostPort(18100))
    resp = await client.post(
        "/api/admin/dashboard/setup-link",
        headers={**_auth(client._tok), "Host": "homeassistant.local:8123"},
    )
    assert resp.status == 200
    parts = urlsplit((await resp.json())["url"])
    assert parts.netloc == "homeassistant.local:18100"
    assert parts.path == "/enter"


async def test_revoke_sessions_bumps_the_epoch(client):
    svc = client.app[dashboard_settings_service_key]
    before = await svc.epoch()
    resp = await client.delete(
        "/api/admin/dashboard/sessions", headers=_auth(client._tok)
    )
    assert resp.status == 204
    assert await svc.epoch() == before + 1


async def test_password_body_must_be_an_object(client):
    resp = await client.put(
        "/api/admin/dashboard/password",
        headers=_auth(client._tok),
        json=["nope"],
    )
    assert resp.status == 422
    assert (await resp.json())["error"]["code"] == "DASHBOARD_SETTINGS_INVALID"


# ── Privacy-lock PIN ────────────────────────────────────────────────────


async def _put_pin(client, body):
    return await client.put(
        "/api/admin/dashboard/pin", headers=_auth(client._tok), json=body
    )


async def _state(client) -> dict:
    resp = await client.get("/api/admin/dashboard", headers=_auth(client._tok))
    return await resp.json()


async def test_set_pin(client):
    svc = client.app[dashboard_settings_service_key]
    resp = await _put_pin(client, {"pin": "4829"})
    assert resp.status == 204
    assert (await _state(client))["pin_set"] is True
    assert await svc.verify_pin("4829")


async def test_set_pin_does_not_sign_tablets_out(client):
    svc = client.app[dashboard_settings_service_key]
    before = await svc.epoch()
    assert (await _put_pin(client, {"pin": "4829"})).status == 204
    assert await svc.epoch() == before


async def test_pin_never_leaves_the_server(client):
    await _put_pin(client, {"pin": "482913"})
    raw = await (
        await client.get("/api/admin/dashboard", headers=_auth(client._tok))
    ).text()
    assert "482913" not in raw
    assert "scrypt" not in raw
    assert "pin_hash" not in raw


@pytest.mark.parametrize(
    "body",
    [
        {"pin": "123"},
        {"pin": "1234567"},
        {"pin": "12a4"},
        {"pin": "١٢٣٤"},
        {"pin": 1234},
        {"pin": None},
        {},
        ["1234"],
    ],
)
async def test_invalid_pin_is_422(client, body):
    resp = await _put_pin(client, body)
    assert resp.status == 422
    err = (await resp.json())["error"]
    assert err["code"] == "DASHBOARD_PIN_INVALID"
    assert err["params"] == {"min": 4, "max": 6}
    assert (await _state(client))["pin_set"] is False


async def test_delete_pin(client):
    svc = client.app[dashboard_settings_service_key]
    await _put_pin(client, {"pin": "4829"})
    resp = await client.delete("/api/admin/dashboard/pin", headers=_auth(client._tok))
    assert resp.status == 204
    assert (await _state(client))["pin_set"] is False
    assert not await svc.verify_pin("4829")


async def test_delete_pin_when_none_is_204(client):
    resp = await client.delete("/api/admin/dashboard/pin", headers=_auth(client._tok))
    assert resp.status == 204


async def test_put_state_reports_pin_set(client):
    await _put_pin(client, {"pin": "4829"})
    body = await (await _put(client, {"locale": "de"})).json()
    assert body["pin_set"] is True


async def test_pin_locks_and_unlocks_a_real_tablet(client):
    """End to end on the dashboard's own port: lock, reload, unlock."""
    await _set_password(client)
    await _put_pin(client, {"pin": "4829"})
    port = _free_port()
    assert (await _put(client, {"enabled": True, "port": port})).status == 200
    origin = f"http://127.0.0.1:{port}"
    form = {"Origin": origin}
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as browser:
        async with browser.post(
            f"{origin}/login",
            data={"password": PASSWORD, "keep": "1"},
            headers=form,
        ) as resp:
            assert resp.status == 200
            assert 'action="/lock"' in await resp.text()
        async with browser.post(f"{origin}/lock", headers=form) as resp:
            assert resp.status == 200
            html = await resp.text()
            assert "data-lock" in html and 'action="/lock"' not in html
        async with browser.get(f"{origin}/") as resp:
            assert "data-lock" in await resp.text()
        async with browser.post(
            f"{origin}/unlock", data={"pin": "0000"}, headers=form
        ) as resp:
            # Post/Redirect/Get: lands on the lock screen with the flag.
            assert resp.status == 200
            assert resp.url.path_qs == "/?pin=wrong"
            html = await resp.text()
            assert "data-lock" in html and "Wrong PIN" in html
        async with browser.post(
            f"{origin}/unlock", data={"pin": "4829"}, headers=form
        ) as resp:
            assert resp.status == 200
            assert "data-lock" not in await resp.text()


# ── Wall panels (displays) ──────────────────────────────────────────────

DISPLAYS = "/api/admin/dashboard/displays"


async def _running(client) -> str:
    """Password + dashboard on a free port; the dashboard's origin."""
    await _set_password(client)
    port = _free_port()
    assert (await _put(client, {"enabled": True, "port": port})).status == 200
    return f"http://127.0.0.1:{port}"


async def _panel_start(origin: str) -> dict:
    """What a wall panel does first: ask the dashboard for a code."""
    async with aiohttp.ClientSession() as panel:
        async with panel.post(
            f"{origin}/api/display/v1/pair",
            json={"model": "esp32-s3-box", "firmware": "0.1.0"},
        ) as resp:
            assert resp.status == 201
            return await resp.json()


async def _panel_get(origin: str, path: str, token: str | None = None):
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as panel:
        async with panel.get(f"{origin}{path}", headers=headers) as resp:
            body = await resp.json() if resp.status != 405 else None
            return resp.status, body


async def _add(client, code: str, name: str = "Kitchen"):
    return await client.post(
        DISPLAYS, headers=_auth(client._tok), json={"code": code, "name": name}
    )


async def test_displays_empty(client):
    resp = await client.get(DISPLAYS, headers=_auth(client._tok))
    assert resp.status == 200
    assert await resp.json() == {"displays": []}
    assert (await _state(client))["displays"] == []


async def test_pair_a_wall_panel_end_to_end(client):
    """Panel asks for a code on the dashboard port → admin types it →
    panel collects its token once → the token opens the panel API →
    rename → remove revokes it."""
    origin = await _running(client)
    started = await _panel_start(origin)
    status, body = await _panel_get(
        origin, f"/api/display/v1/pair/{started['pair_id']}"
    )
    assert (status, body) == (200, {"status": "pending"})

    resp = await _add(client, started["code"].lower())
    assert resp.status == 201
    display = await resp.json()
    assert set(display) == {
        "id",
        "name",
        "model",
        "firmware",
        "created_at",
        "last_seen_at",
        "locked",
    }
    assert display["name"] == "Kitchen"
    assert display["model"] == "esp32-s3-box"

    status, body = await _panel_get(
        origin, f"/api/display/v1/pair/{started['pair_id']}"
    )
    assert status == 200 and body["status"] == "paired"
    assert body["display_id"] == display["id"]
    token = body["token"]
    status, _ = await _panel_get(origin, f"/api/display/v1/pair/{started['pair_id']}")
    assert status == 404

    status, body = await _panel_get(origin, "/api/display/v1/whoami", token)
    assert (status, body) == (200, {"display_id": display["id"], "name": "Kitchen"})

    listed = await (await client.get(DISPLAYS, headers=_auth(client._tok))).json()
    assert [d["id"] for d in listed["displays"]] == [display["id"]]
    assert listed["displays"][0]["last_seen_at"] is not None
    state = await _state(client)
    assert [d["id"] for d in state["displays"]] == [display["id"]]
    # The token and its hash never reach the admin API.
    for text in (str(listed), str(state)):
        assert token not in text
        assert "token_hash" not in text

    resp = await client.patch(
        f"{DISPLAYS}/{display['id']}",
        headers=_auth(client._tok),
        json={"name": "Hallway"},
    )
    assert resp.status == 200
    assert (await resp.json())["name"] == "Hallway"

    resp = await client.delete(
        f"{DISPLAYS}/{display['id']}", headers=_auth(client._tok)
    )
    assert resp.status == 204
    status, _ = await _panel_get(origin, "/api/display/v1/whoami", token)
    assert status == 401


async def test_sign_out_all_tablets_unpairs_panels(client):
    origin = await _running(client)
    started = await _panel_start(origin)
    assert (await _add(client, started["code"])).status == 201
    _, body = await _panel_get(origin, f"/api/display/v1/pair/{started['pair_id']}")
    resp = await client.delete(
        "/api/admin/dashboard/sessions", headers=_auth(client._tok)
    )
    assert resp.status == 204
    status, _ = await _panel_get(origin, "/api/display/v1/whoami", body["token"])
    assert status == 401
    assert (await _state(client))["displays"] == []


async def test_add_display_wrong_code(client):
    await _running(client)
    resp = await _add(client, "ZZZZZZ")
    assert resp.status == 404
    assert (await resp.json())["error"]["code"] == "DASHBOARD_PAIRING_CODE_INVALID"


async def test_add_display_wrong_codes_are_throttled(client):
    await _running(client)
    for _ in range(5):
        assert (await _add(client, "ZZZZZZ")).status == 404
    resp = await _add(client, "ZZZZZZ")
    assert resp.status == 429
    body = await resp.json()
    assert body["error"]["code"] == "DASHBOARD_PAIRING_THROTTLED"
    assert body["error"]["params"]["retry_after"] > 0


async def test_add_display_while_disabled(client):
    await _set_password(client)
    resp = await _add(client, "ABCDEF")
    assert resp.status == 409
    assert (await resp.json())["error"]["code"] == "DASHBOARD_DISABLED"


@pytest.mark.parametrize(
    "body", [{"code": "ABCDEF"}, {"code": "ABCDEF", "name": ""}, ["x"]]
)
async def test_add_display_invalid_name(client, body):
    await _running(client)
    resp = await client.post(DISPLAYS, headers=_auth(client._tok), json=body)
    assert resp.status == 422
    err = (await resp.json())["error"]
    assert err["code"] == "DASHBOARD_SETTINGS_INVALID"
    assert err["params"] == {"field": "name"}


async def test_rename_unknown_display(client):
    resp = await client.patch(
        f"{DISPLAYS}/nope", headers=_auth(client._tok), json={"name": "x"}
    )
    assert resp.status == 404
    assert (await resp.json())["error"]["code"] == "DASHBOARD_DISPLAY_NOT_FOUND"


async def test_remove_unknown_display(client):
    resp = await client.delete(f"{DISPLAYS}/nope", headers=_auth(client._tok))
    assert resp.status == 404


# ── wall-panel privacy lock ──────────────────────────────────────────────


async def _panel_post(origin: str, path: str, token: str, body: object = None):
    async with aiohttp.ClientSession() as panel:
        async with panel.post(
            f"{origin}{path}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            return resp.status, await resp.json()


async def _paired_panel(client) -> tuple[str, str, str]:
    """``(origin, display_id, token)`` of a panel paired end to end."""
    origin = await _running(client)
    started = await _panel_start(origin)
    display = await (await _add(client, started["code"])).json()
    _, body = await _panel_get(origin, f"/api/display/v1/pair/{started['pair_id']}")
    return origin, display["id"], body["token"]


async def _listed_locked(client, display_id: str) -> bool:
    listed = await (await client.get(DISPLAYS, headers=_auth(client._tok))).json()
    return next(d["locked"] for d in listed["displays"] if d["id"] == display_id)


async def test_panel_lock_unlock_and_admin_recovery_end_to_end(client):
    origin, display_id, token = await _paired_panel(client)
    # No PIN yet: nothing could unlock it.
    status, body = await _panel_post(origin, "/api/display/v1/lock", token)
    assert status == 409 and body["error"]["code"] == "DASHBOARD_PIN_REQUIRED"

    assert (await _put_pin(client, {"pin": "4829"})).status == 204
    status, body = await _panel_post(origin, "/api/display/v1/lock", token)
    assert status == 200 and body["locked"] is True
    assert await _listed_locked(client, display_id)
    assert (await _state(client))["displays"][0]["locked"] is True
    status, _ = await _panel_get(origin, "/api/display/v1/home", token)
    assert status == 423

    status, body = await _panel_post(
        origin, "/api/display/v1/unlock", token, {"pin": "0000"}
    )
    assert status == 403 and body["error"]["params"] == {"remaining": 4}
    status, body = await _panel_post(
        origin, "/api/display/v1/unlock", token, {"pin": "4829"}
    )
    assert (status, body) == (200, {"api_version": 1, "locked": False})
    assert not await _listed_locked(client, display_id)

    # Admin recovery: PATCH {locked: false}.
    await _panel_post(origin, "/api/display/v1/lock", token)
    resp = await client.patch(
        f"{DISPLAYS}/{display_id}", headers=_auth(client._tok), json={"locked": False}
    )
    assert resp.status == 200
    shown = await resp.json()
    assert shown["locked"] is False and shown["name"] == "Kitchen"
    status, _ = await _panel_get(origin, "/api/display/v1/whoami", token)
    assert status == 200

    # Removing the PIN lifts every lock; a new PIN doesn't bring it back.
    await _panel_post(origin, "/api/display/v1/lock", token)
    assert await _listed_locked(client, display_id)
    resp = await client.delete("/api/admin/dashboard/pin", headers=_auth(client._tok))
    assert resp.status == 204
    assert not await _listed_locked(client, display_id)
    assert (await _put_pin(client, {"pin": "1111"})).status == 204
    status, _ = await _panel_get(origin, "/api/display/v1/whoami", token)
    assert status == 200


async def test_admin_patch_renames_and_unlocks_together(client):
    origin, display_id, token = await _paired_panel(client)
    await _put_pin(client, {"pin": "4829"})
    await _panel_post(origin, "/api/display/v1/lock", token)
    resp = await client.patch(
        f"{DISPLAYS}/{display_id}",
        headers=_auth(client._tok),
        json={"name": "Hall", "locked": False},
    )
    assert resp.status == 200
    shown = await resp.json()
    assert (shown["name"], shown["locked"]) == ("Hall", False)


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"locked": True}, "locked"),
        ({"locked": "false"}, "locked"),
        ({"locked": None}, "locked"),
        ({"name": "Hall", "locked": True}, "locked"),
        ({}, "name"),
    ],
)
async def test_admin_patch_refuses_anything_but_unlock(client, body, field):
    origin, display_id, token = await _paired_panel(client)
    resp = await client.patch(
        f"{DISPLAYS}/{display_id}", headers=_auth(client._tok), json=body
    )
    assert resp.status == 422
    err = (await resp.json())["error"]
    assert err["code"] == "DASHBOARD_SETTINGS_INVALID"
    assert err["params"] == {"field": field}
    # Nothing was written — not even the name of a mixed body.
    listed = await (await client.get(DISPLAYS, headers=_auth(client._tok))).json()
    assert (listed["displays"][0]["name"], listed["displays"][0]["locked"]) == (
        "Kitchen",
        False,
    )


async def test_admin_unlock_unknown_display(client):
    resp = await client.patch(
        f"{DISPLAYS}/nope", headers=_auth(client._tok), json={"locked": False}
    )
    assert resp.status == 404
