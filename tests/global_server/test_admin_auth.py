"""Tests for the GFS admin auth flow (login / session / brute-force)."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.global_server.admin import (
    BRUTE_FORCE_MAX_ATTEMPTS,
    SESSION_COOKIE,
    hash_password,
    verify_password,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.server import create_gfs_app


def _test_config(tmp_dir):
    return GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id="gfs-test",
    )


@pytest.fixture
async def client(tmp_dir):
    app = create_gfs_app(_test_config(tmp_dir))
    async with TestClient(TestServer(app)) as tc:
        # Seed the admin password via the already-wired admin repo —
        # equivalent to `socialhome-global-server --set-password`.
        from socialhome.global_server.app_keys import gfs_admin_repo_key

        await app[gfs_admin_repo_key].set_config(
            "admin_password_hash",
            hash_password("test-pw-123"),
        )
        yield tc


async def test_hash_and_verify_password_roundtrip():
    h = hash_password("hello-world")
    assert verify_password("hello-world", h) is True
    assert verify_password("wrong", h) is False
    assert verify_password("", h) is False


async def test_login_without_password_set_returns_503(tmp_dir):
    app = create_gfs_app(_test_config(tmp_dir))
    async with TestClient(TestServer(app)) as tc:
        resp = await tc.post("/admin/login", json={"password": "x"})
        assert resp.status == 503


async def test_login_success_sets_session_cookie(client):
    resp = await client.post("/admin/login", json={"password": "test-pw-123"})
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"
    assert SESSION_COOKIE in resp.cookies


async def test_login_wrong_password_returns_401(client):
    resp = await client.post("/admin/login", json={"password": "nope"})
    assert resp.status == 401


async def test_brute_force_lockout(client):
    for _ in range(BRUTE_FORCE_MAX_ATTEMPTS):
        await client.post("/admin/login", json={"password": "nope"})
    # Sixth attempt → 429.
    resp = await client.post("/admin/login", json={"password": "nope"})
    assert resp.status == 429
    assert "Retry-After" in resp.headers


async def test_admin_api_requires_cookie(client):
    resp = await client.get("/admin/api/overview")
    assert resp.status == 401


async def test_admin_api_accessible_after_login(client):
    ok = await client.post("/admin/login", json={"password": "test-pw-123"})
    assert ok.status == 200
    resp = await client.get("/admin/api/overview")
    assert resp.status == 200
    body = await resp.json()
    assert "clients" in body
    assert "spaces" in body


async def test_logout_clears_cookie(client):
    await client.post("/admin/login", json={"password": "test-pw-123"})
    resp = await client.post("/admin/logout")
    assert resp.status == 200
    # Subsequent API call fails — cookie cleared.
    resp = await client.get("/admin/api/overview")
    assert resp.status == 401
