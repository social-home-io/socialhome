"""Tests for GET /healthz — health check endpoint."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from social_home.app import create_app
from social_home.config import Config


@pytest.fixture
async def client(tmp_dir):
    """Minimal app client (no auth seeding needed for health check)."""
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
    )
    app = create_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        yield tc


async def test_healthz_returns_200(client):
    """GET /healthz returns HTTP 200."""
    resp = await client.get("/healthz")
    assert resp.status == 200


async def test_healthz_returns_ok_body(client):
    """GET /healthz returns ``status: ok`` plus subsystem probes."""
    resp = await client.get("/healthz")
    body = await resp.json()
    assert body["status"] == "ok"
    # Subsystems probed in healthz: db, ws_clients, outbox_depth.
    subs = body["subsystems"]
    assert subs["db"] == "ok"
    assert "ws_clients" in subs
    assert "outbox_depth" in subs


async def test_healthz_db_failure_returns_503(client):
    """A broken DB probe surfaces as 503."""
    from social_home.app_keys import db_key

    real_db = client.app[db_key]

    class _BrokenDb:
        async def fetchone(self, *_a, **_kw):
            raise RuntimeError("simulated DB outage")

    client.app[db_key] = _BrokenDb()
    try:
        resp = await client.get("/healthz")
        assert resp.status == 503
        body = await resp.json()
        assert body["status"] == "fail"
        assert body["subsystems"]["db"] == "fail"
    finally:
        client.app[db_key] = real_db


async def test_healthz_no_auth_required(client):
    """GET /healthz is publicly accessible without authentication."""
    resp = await client.get("/healthz")
    assert resp.status != 401
