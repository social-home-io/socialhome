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
)
from socialhome.dashboard.auth import COOKIE_NAME
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
    assert body["address"] is None
    host = urlsplit(f"http://{client.host}").hostname
    assert body["url"] == f"http://{host}:8100"
    assert body["locale"] == "en"
    assert body["idle_return_minutes"] == 3
    assert body["night_start"] == "22:00"
    assert body["night_end"] == "06:00"
    assert body["keep_screen_on"] is True
    assert body["show_status_text"] is False
    assert body["url_host_allowed"] is True
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
        ("kid", "uid-kid", "Kid", 1, 1, 9, "abc123"),
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
