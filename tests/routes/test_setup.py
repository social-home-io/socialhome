"""Tests for the /api/setup/* first-boot wizard endpoints."""

from __future__ import annotations

from socialhome.app import create_app
from socialhome.app_keys import setup_service_key
from socialhome.app_keys import db_key as _db_key
from socialhome.config import Config


async def _build_standalone_app(aiohttp_client, tmp_dir):
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "t.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
    )
    app = create_app(cfg)
    tc = await aiohttp_client(app)
    tc._app = app
    return tc


# ── Standalone ──────────────────────────────────────────────────────────────


async def test_standalone_setup_seeds_admin_and_returns_token(
    aiohttp_client,
    tmp_dir,
):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    r = await tc.post(
        "/api/setup/standalone",
        json={"username": "owner", "password": "hunter2"},
    )
    assert r.status == 201, await r.text()
    body = await r.json()
    assert isinstance(body["token"], str) and len(body["token"]) > 20
    db = tc._app[_db_key]
    pu = await db.fetchone(
        "SELECT * FROM platform_users WHERE username='owner'",
    )
    assert pu is not None and pu["is_admin"] == 1
    assert await tc._app[setup_service_key].is_required() is False


async def test_standalone_setup_requires_username_and_password(
    aiohttp_client,
    tmp_dir,
):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    r = await tc.post("/api/setup/standalone", json={"username": "x"})
    assert r.status == 422


async def test_standalone_setup_locked_after_completion(
    aiohttp_client,
    tmp_dir,
):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    r1 = await tc.post(
        "/api/setup/standalone",
        json={"username": "owner", "password": "pw"},
    )
    assert r1.status == 201
    r2 = await tc.post(
        "/api/setup/standalone",
        json={"username": "owner2", "password": "pw"},
    )
    assert r2.status == 409
    body = await r2.json()
    assert body["error"]["code"] == "ALREADY_COMPLETE"


# ── ha (mode mismatch + happy path) ─────────────────────────────────────────


async def test_ha_owner_setup_mode_mismatch_in_standalone(
    aiohttp_client,
    tmp_dir,
):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    r = await tc.post(
        "/api/setup/ha/owner",
        json={"username": "alice"},
    )
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "WRONG_MODE"


async def test_haos_complete_setup_mode_mismatch_in_standalone(
    aiohttp_client,
    tmp_dir,
):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    r = await tc.post("/api/setup/haos/complete")
    assert r.status == 409


async def test_ha_persons_mode_mismatch_in_standalone(aiohttp_client, tmp_dir):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    r = await tc.get("/api/setup/ha/persons")
    assert r.status == 409
