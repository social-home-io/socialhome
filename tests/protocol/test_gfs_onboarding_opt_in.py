"""§27.9 release blocker: the onboarding GFS step is strictly opt-in.

A household ships with a default GFS (``[gfs] default_url``) so onboarding
can offer a one-click "Connect to the GFS" step. Merely having that default
must not make the household talk to it: booting, signing in, asking whether
to offer the step, and finishing (or skipping) onboarding all send NOTHING
to any GFS. Only an admin's explicit yes (``POST /api/gfs/connections/
default``) does, and then only what QR pairing already sends.
"""

from __future__ import annotations

from types import MappingProxyType

import pytest
from aiohttp import web

from socialhome.app import create_app
from socialhome.app_keys import db_key
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.crypto import derive_user_id

pytestmark = pytest.mark.security


@pytest.fixture
async def spy_gfs(aiohttp_server):
    """A stand-in GFS that records every request it receives."""
    seen: list[str] = []

    async def record(request: web.Request) -> web.Response:
        seen.append(f"{request.method} {request.path}")
        if request.path == "/gfs/info":
            # A descriptor without a signed capability block.
            return web.json_response(
                {"gfs_instance_id": "spy", "public_key": "aa" * 32}
            )
        return web.json_response({}, status=404)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", record)
    server = await aiohttp_server(app)
    server.seen = seen  # type: ignore[attr-defined]
    return server


async def _household(aiohttp_client, tmp_dir, default_url: str):
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
        gfs_default_url=default_url,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://home.example"})}
        ),
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
    return tc


AUTH = {"Authorization": "Bearer admin-tok"}


async def test_declining_the_gfs_step_sends_nothing_to_any_gfs(
    aiohttp_client, tmp_dir, spy_gfs
):
    default_url = str(spy_gfs.make_url("")).rstrip("/")
    home = await _household(aiohttp_client, tmp_dir, default_url)

    # The onboarding walk as the SPA performs it when the admin declines.
    assert (await home.get("/api/me", headers=AUTH)).status == 200
    offer = await (await home.get("/api/gfs/connections/default", headers=AUTH)).json()
    assert offer["available"] is True
    assert offer["url"] == default_url
    done = await home.post("/api/me/onboarding-complete", headers=AUTH)
    assert done.status in (200, 204)
    assert (await home.get("/api/gfs/connections", headers=AUTH)).status == 200

    assert spy_gfs.seen == []


async def test_saying_yes_is_what_contacts_the_gfs(aiohttp_client, tmp_dir, spy_gfs):
    """The control: the spy does see the household once the admin opts in —
    so the empty list above is a real negative, not a broken spy."""
    default_url = str(spy_gfs.make_url("")).rstrip("/")
    home = await _household(aiohttp_client, tmp_dir, default_url)
    r = await home.post("/api/gfs/connections/default", headers=AUTH)
    # The spy offers no signed capability block, so sign-up is "closed" — and
    # the household stopped after the descriptor fetch.
    assert r.status == 409
    assert spy_gfs.seen == ["GET /gfs/info"]
