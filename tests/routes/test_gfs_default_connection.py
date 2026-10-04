"""HTTP tests for ``GET/POST /api/gfs/connections/default``.

The onboarding "Connect to the GFS" step. ``GET`` tells the SPA whether to
offer the step (local facts only — it never contacts the GFS); ``POST``
pairs with the configured default GFS through its open sign-up. Both are
admin-only. The household runs against the REAL GFS app.
"""

from __future__ import annotations

import socket
from pathlib import Path
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.crypto import derive_user_id
from socialhome.global_server.app_keys import gfs_admin_repo_key, gfs_fed_repo_key
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.server import create_gfs_app

from .conftest import _auth

PATH = "/api/gfs/connections/default"


async def _gfs(aiohttp_client, tmp_dir: Path, *, open_signup: bool = True):
    data = tmp_dir / "gfs"
    data.mkdir(exist_ok=True)
    cfg = GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(data),
        instance_id="gfs-default",
        open_signup=open_signup,
    )
    return await aiohttp_client(create_gfs_app(cfg))


def _url(tc) -> str:
    return str(tc.make_url("")).rstrip("/")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _household(
    aiohttp_client,
    tmp_dir: Path,
    *,
    default_url: str,
    external_url: str | None = "https://home.example",
):
    home = tmp_dir / "home"
    home.mkdir(exist_ok=True)
    options = (
        MappingProxyType(
            {"standalone": MappingProxyType({"external_url": external_url})}
        )
        if external_url
        else MappingProxyType({})
    )
    cfg = Config(
        data_dir=str(home),
        db_path=str(home / "test.db"),
        media_path=str(home / "media"),
        apps_path=str(home / "apps"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
        instance_name="Alpha House",
        gfs_default_url=default_url,
        platform_options=options,
    )
    tc = await aiohttp_client(create_app(cfg))
    db = tc.app[db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'"
    )
    uid = derive_user_id(bytes.fromhex(row["identity_public_key"]), "admin")
    await db.enqueue(
        "INSERT OR REPLACE INTO users(username, user_id, display_name, is_admin) "
        "VALUES(?,?,?,1)",
        ("admin", uid, "Admin"),
    )
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t1", uid, "t", sha256_token_hash("admin-tok")),
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES('bob', 'bob-id', 'Bob', 0)",
    )
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES('tb', 'bob-id', 't', ?)",
        (sha256_token_hash("bob-tok"),),
    )
    return tc


ADMIN = _auth("admin-tok")
MEMBER = _auth("bob-tok")


# ─── GET ─────────────────────────────────────────────────────────────


async def test_get_offers_the_default_gfs(aiohttp_client, tmp_dir):
    home = await _household(
        aiohttp_client, tmp_dir, default_url="https://gfs.social-home.io"
    )
    r = await home.get(PATH, headers=ADMIN)
    assert r.status == 200
    assert await r.json() == {
        "url": "https://gfs.social-home.io",
        "available": True,
        "reason": None,
        "connection": None,
    }


async def test_get_empty_default_url_hides_the_step(aiohttp_client, tmp_dir):
    home = await _household(aiohttp_client, tmp_dir, default_url="")
    body = await (await home.get(PATH, headers=ADMIN)).json()
    assert body["available"] is False
    assert body["reason"] == "disabled"
    assert body["url"] == ""


async def test_get_without_external_url_explains_why(aiohttp_client, tmp_dir):
    home = await _household(
        aiohttp_client,
        tmp_dir,
        default_url="https://gfs.social-home.io",
        external_url=None,
    )
    body = await (await home.get(PATH, headers=ADMIN)).json()
    assert body["available"] is False
    assert body["reason"] == "no_external_url"


async def test_get_is_admin_only(aiohttp_client, tmp_dir):
    home = await _household(
        aiohttp_client, tmp_dir, default_url="https://gfs.social-home.io"
    )
    assert (await home.get(PATH, headers=MEMBER)).status == 403
    assert (await home.get(PATH)).status == 401


# ─── POST ────────────────────────────────────────────────────────────


async def test_post_connects_through_open_signup(aiohttp_client, tmp_dir):
    gfs = await _gfs(aiohttp_client, tmp_dir)
    home = await _household(aiohttp_client, tmp_dir, default_url=_url(gfs))
    r = await home.post(PATH, headers=ADMIN)
    assert r.status == 201, await r.text()
    body = await r.json()
    assert body["status"] == "active"
    assert body["inbox_url"] == _url(gfs)
    assert "public_key" not in body

    # The GFS registered this household with its External URL inbox.
    instances = await gfs.server.app[gfs_fed_repo_key].list_instances()
    assert len(instances) == 1
    assert instances[0].inbox_url.startswith("https://home.example")
    assert instances[0].display_name == "Alpha House"

    # It now shows in the list, and GET reports it (no second offer).
    listed = await (await home.get("/api/gfs/connections", headers=ADMIN)).json()
    assert [c["id"] for c in listed] == [body["id"]]
    state = await (await home.get(PATH, headers=ADMIN)).json()
    assert state["available"] is False
    assert state["reason"] == "already_connected"
    assert state["connection"]["status"] == "active"

    again = await home.post(PATH, headers=ADMIN)
    assert again.status == 409
    assert (await again.json())["error"]["code"] == "ALREADY_CONNECTED"


async def test_post_is_pending_when_the_gfs_approves_households(
    aiohttp_client, tmp_dir
):
    gfs = await _gfs(aiohttp_client, tmp_dir)
    await gfs.server.app[gfs_admin_repo_key].set_config("auto_accept_clients", "0")
    home = await _household(aiohttp_client, tmp_dir, default_url=_url(gfs))
    r = await home.post(PATH, headers=ADMIN)
    assert r.status == 201
    assert (await r.json())["status"] == "pending"
    state = await (await home.get(PATH, headers=ADMIN)).json()
    assert state["connection"]["status"] == "pending"


async def test_post_signup_closed(aiohttp_client, tmp_dir):
    gfs = await _gfs(aiohttp_client, tmp_dir, open_signup=False)
    home = await _household(aiohttp_client, tmp_dir, default_url=_url(gfs))
    r = await home.post(PATH, headers=ADMIN)
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "GFS_SIGNUP_CLOSED"
    assert await gfs.server.app[gfs_fed_repo_key].list_instances() == []


async def test_post_gfs_busy(aiohttp_client, tmp_dir):
    """The GFS hands one token per address per interval: a second sign-up
    right after (here: after disconnecting) is 'busy', not an error."""
    gfs = await _gfs(aiohttp_client, tmp_dir)
    home = await _household(aiohttp_client, tmp_dir, default_url=_url(gfs))
    first = await (await home.post(PATH, headers=ADMIN)).json()
    await home.delete(f"/api/gfs/connections/{first['id']}", headers=ADMIN)
    r = await home.post(PATH, headers=ADMIN)
    assert r.status == 503
    assert (await r.json())["error"]["code"] == "GFS_BUSY"


async def test_post_gfs_unreachable(aiohttp_client, tmp_dir):
    home = await _household(
        aiohttp_client, tmp_dir, default_url=f"http://127.0.0.1:{_free_port()}"
    )
    r = await home.post(PATH, headers=ADMIN)
    assert r.status == 502
    assert (await r.json())["error"]["code"] == "GFS_UNREACHABLE"


async def test_post_insecure_default_url(aiohttp_client, tmp_dir):
    home = await _household(
        aiohttp_client, tmp_dir, default_url="http://gfs.example.org"
    )
    r = await home.post(PATH, headers=ADMIN)
    assert r.status == 422
    assert (await r.json())["error"]["code"] == "GFS_PAIRING_FAILED"


async def test_post_disabled(aiohttp_client, tmp_dir):
    home = await _household(aiohttp_client, tmp_dir, default_url="")
    r = await home.post(PATH, headers=ADMIN)
    assert r.status == 404
    assert (await r.json())["error"]["code"] == "GFS_DEFAULT_DISABLED"


async def test_post_without_external_url(aiohttp_client, tmp_dir):
    gfs = await _gfs(aiohttp_client, tmp_dir)
    home = await _household(
        aiohttp_client, tmp_dir, default_url=_url(gfs), external_url=None
    )
    r = await home.post(PATH, headers=ADMIN)
    assert r.status == 422
    assert (await r.json())["error"]["code"] == "NOT_CONFIGURED"
    assert await gfs.server.app[gfs_fed_repo_key].list_instances() == []


@pytest.mark.parametrize("headers", [MEMBER, {}])
async def test_post_is_admin_only(aiohttp_client, tmp_dir, headers):
    gfs = await _gfs(aiohttp_client, tmp_dir)
    home = await _household(aiohttp_client, tmp_dir, default_url=_url(gfs))
    r = await home.post(PATH, headers=headers)
    assert r.status in (401, 403)
    assert await gfs.server.app[gfs_fed_repo_key].list_instances() == []
