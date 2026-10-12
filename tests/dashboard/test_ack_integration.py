"""End to end: a household-chat mention on the wall, acknowledged with
"Got it" (``POST /ack``).

The whole app (``create_app``: real ``HouseholdDashboardService``,
``HouseholdChatService``, ``DmService``, SQLite in ``tmp_path``) with the
dashboard listening on its own port — driven over HTTP like a tablet:
session cookie, same-origin ``POST``. Checks the ✅ row lands in the
chat and the banner / badge is gone from the next fragment.
"""

from __future__ import annotations

import socket
from types import MappingProxyType
from urllib.parse import quote

import aiohttp
import pytest

from socialhome.app import create_app
from socialhome.app_keys import dashboard_server_key, db_key
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.crypto import derive_user_id
from socialhome.dashboard.auth import COOKIE_NAME
from socialhome.domain.household_dashboard import ACK_EMOJI
from socialhome.domain.user import DASHBOARD_AUTHOR

PASSWORD = "dashboard-pass"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _token(db, name: str, user_id: str) -> str:
    raw = f"{name}-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        (f"t-{name}", user_id, "t", sha256_token_hash(raw)),
    )
    return raw


@pytest.fixture
async def stack(aiohttp_client, tmp_path):
    cfg = Config(
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "test.db"),
        media_path=str(tmp_path / "media"),
        apps_path=str(tmp_path / "apps"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
        listen_host="127.0.0.1",
        platform_options=MappingProxyType(
            {
                "standalone": MappingProxyType(
                    {"external_url": "https://test.example"}
                ),
            }
        ),
    )
    app = create_app(cfg)
    tc = await aiohttp_client(app)
    db = app[db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'"
    )
    admin_uid = derive_user_id(bytes.fromhex(row["identity_public_key"]), "admin")
    await db.enqueue(
        "INSERT OR REPLACE INTO users(username, user_id, display_name, is_admin) "
        "VALUES(?,?,?,1)",
        ("admin", admin_uid, "Admin"),
    )
    admin = await _token(db, "admin", admin_uid)
    users: dict[str, tuple[str, str]] = {}
    for name in ("anna", "leo"):
        resp = await tc.post(
            "/api/admin/users",
            json={
                "username": name,
                "password": "password123",
                "display_name": name.title(),
            },
            headers=_auth(admin),
        )
        assert resp.status == 201, await resp.text()
        uid = (await resp.json())["user_id"]
        users[name] = (uid, await _token(db, name, uid))
    port = _free_port()
    resp = await tc.put(
        "/api/admin/dashboard/password",
        json={"password": PASSWORD},
        headers=_auth(admin),
    )
    assert resp.status in (200, 204), await resp.text()
    resp = await tc.put(
        "/api/admin/dashboard",
        json={
            "enabled": True,
            "port": port,
            "show_household_messages": True,
            # An empty night window: never night, whatever the clock says.
            "night_start": "00:00",
            "night_end": "00:00",
            "people": {admin_uid: {"show": False}},
        },
        headers=_auth(admin),
    )
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["running"]
    tc._users = users
    tc._dash = f"http://127.0.0.1:{port}"
    yield tc
    await app[dashboard_server_key].stop()


async def _send(tc, who: str, chat_id: str, text: str) -> str:
    resp = await tc.post(
        f"/api/conversations/{chat_id}/messages",
        json={"content": text},
        headers=_auth(tc._users[who][1]),
    )
    assert resp.status == 201, await resp.text()
    return (await resp.json())["id"]


async def _reactions(tc, chat_id: str, message_id: str) -> list[tuple[str, str]]:
    resp = await tc.get(
        f"/api/conversations/{chat_id}/messages", headers=_auth(tc._users["anna"][1])
    )
    for m in await resp.json():
        if m["id"] == message_id:
            return [(r["user_id"], r["emoji"]) for r in m.get("reactions") or []]
    raise AssertionError(message_id)


async def test_got_it_reacts_and_clears_the_wall(stack):
    chat = await (
        await stack.get("/api/household/chat", headers=_auth(stack._users["anna"][1]))
    ).json()
    chat_id = chat["conversation_id"]
    here = await _send(stack, "anna", chat_id, "@here Dinner is ready")
    for_leo = await _send(stack, "anna", chat_id, "@leo Shoes on please")
    leo_id = stack._users["leo"][0]
    base = stack._dash
    origin = {"Origin": base}
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as s:
        resp = await s.post(
            f"{base}/login",
            data={"password": PASSWORD, "keep": "1"},
            headers=origin,
            allow_redirects=False,
        )
        assert resp.status == 303
        cookie = {"Cookie": f"{COOKIE_NAME}={resp.cookies[COOKIE_NAME].value}"}

        frag = await (await s.get(f"{base}/fragment/home", headers=cookie)).text()
        assert 'class="dash-alerts"' in frag and "Dinner is ready" in frag
        assert leo_id in frag[frag.index("data-mention-badges") :].split('"')[1]

        # @here on Home: written as the dashboard, the banner goes.
        resp = await s.post(
            f"{base}/ack",
            data={"message_id": here},
            headers={**cookie, **origin},
            allow_redirects=False,
        )
        assert resp.status == 303 and resp.headers["Location"] == "/"
        assert await _reactions(stack, chat_id, here) == [(DASHBOARD_AUTHOR, ACK_EMOJI)]
        frag = await (await s.get(f"{base}/fragment/home", headers=cookie)).text()
        assert "dash-alerts" not in frag

        # @leo on Leo's page: credited to Leo, his card and badge go.
        page = f"/p/{quote(leo_id, safe='')}"
        html = await (await s.get(f"{base}{page}", headers=cookie)).text()
        assert "Shoes on please" in html
        resp = await s.post(
            f"{base}/ack",
            data={"message_id": for_leo, "person_id": leo_id},
            headers={**cookie, **origin},
            allow_redirects=False,
        )
        assert resp.status == 303 and resp.headers["Location"] == page
        assert await _reactions(stack, chat_id, for_leo) == [(leo_id, ACK_EMOJI)]
        frag = await (await s.get(f"{base}/fragment{page}", headers=cookie)).text()
        assert "Shoes on please" not in frag
        assert 'data-mention-badges=""' in frag

        # Anything the wall doesn't show: 404, nothing written.
        resp = await s.post(
            f"{base}/ack",
            data={"message_id": for_leo},  # a person mention is no @here
            headers={**cookie, **origin},
            allow_redirects=False,
        )
        assert resp.status == 404
        assert await _reactions(stack, chat_id, for_leo) == [(leo_id, ACK_EMOJI)]
