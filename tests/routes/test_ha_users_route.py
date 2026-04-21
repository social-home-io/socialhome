"""Tests for /api/admin/ha-users + provision/deprovision admin routes."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from social_home.app import create_app
from dataclasses import replace

from social_home.app_keys import (
    config_key,
    db_key as _db_key,
    platform_adapter_key,
)
from social_home.auth import sha256_token_hash
from social_home.config import Config
from social_home.crypto import derive_user_id
from social_home.platform.adapter import ExternalUser


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class _FakeHaAdapter:
    """Minimal stub exposing the HA-specific interface the route needs."""

    def __init__(self, users):
        self._users = {u.username: u for u in users}

    async def list_external_users(self):
        return list(self._users.values())

    async def get_external_user(self, username):
        return self._users.get(username)


@pytest.fixture
async def client(tmp_dir):
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
    )
    app = create_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        db = app[_db_key]
        row = await db.fetchone(
            "SELECT identity_public_key FROM instance_identity WHERE id='self'",
        )
        pk = bytes.fromhex(row["identity_public_key"])

        class _KP:
            public_key = pk

        admin_uid = derive_user_id(_KP.public_key, "pascal")
        bob_uid = derive_user_id(_KP.public_key, "bob")
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin,"
            " source) VALUES(?,?,?,1,'ha')",
            ("pascal", admin_uid, "Pascal"),
        )
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin)"
            " VALUES(?,?,?,0)",
            ("bob", bob_uid, "Bob"),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
            " VALUES(?,?,?,?)",
            ("tid-admin", admin_uid, "t", sha256_token_hash("admin-token")),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
            " VALUES(?,?,?,?)",
            ("tid-bob", bob_uid, "t", sha256_token_hash("bob-token")),
        )
        tc._admin_token = "admin-token"
        tc._bob_token = "bob-token"
        yield tc


def _swap_to_ha_adapter(app, users):
    """Replace the standalone adapter with a fake HA adapter exposing users.

    Also flips ``config.mode`` to ``'ha'`` so the route's mode gate opens.
    """
    app[platform_adapter_key] = _FakeHaAdapter(users)
    app[config_key] = replace(app[config_key], mode="ha")


# ── Happy-path tests ─────────────────────────────────────────────────


async def test_list_ha_users_includes_synced_flag(client):
    _swap_to_ha_adapter(
        client.server.app,
        [
            ExternalUser("pascal", "Pascal", None, is_admin=True),
            ExternalUser("kid", "Little Pascal", None, is_admin=False),
        ],
    )
    resp = await client.get(
        "/api/admin/ha-users",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    body = await resp.json()
    # Two HA users returned, pascal is already provisioned (source=ha), kid isn't.
    by_name = {u["username"]: u for u in body}
    assert by_name["pascal"]["synced"] is True
    assert by_name["kid"]["synced"] is False


async def test_provision_ha_user_creates_row(client):
    _swap_to_ha_adapter(
        client.server.app,
        [
            ExternalUser("kid", "Little Pascal", None, is_admin=False),
        ],
    )
    resp = await client.post(
        "/api/admin/ha-users/kid/provision",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["username"] == "kid"
    assert body["synced"] is True
    # List again — kid is now synced.
    listing = await (
        await client.get(
            "/api/admin/ha-users",
            headers=_auth(client._admin_token),
        )
    ).json()
    kid = next(u for u in listing if u["username"] == "kid")
    assert kid["synced"] is True


async def test_provision_unknown_ha_user_404(client):
    _swap_to_ha_adapter(client.server.app, [])
    resp = await client.post(
        "/api/admin/ha-users/ghost/provision",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 404


async def test_deprovision_ha_user(client):
    _swap_to_ha_adapter(
        client.server.app,
        [
            ExternalUser("kid", "Little Pascal", None, is_admin=False),
        ],
    )
    await client.post(
        "/api/admin/ha-users/kid/provision",
        headers=_auth(client._admin_token),
    )
    resp = await client.delete(
        "/api/admin/ha-users/kid/provision",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    # Re-listing: kid is back to synced=False.
    listing = await (
        await client.get(
            "/api/admin/ha-users",
            headers=_auth(client._admin_token),
        )
    ).json()
    kid = next(u for u in listing if u["username"] == "kid")
    assert kid["synced"] is False


async def test_forbidden_for_non_admin(client):
    _swap_to_ha_adapter(
        client.server.app,
        [
            ExternalUser("kid", "Little Pascal", None, is_admin=False),
        ],
    )
    resp = await client.get(
        "/api/admin/ha-users",
        headers=_auth(client._bob_token),
    )
    assert resp.status == 403


async def test_501_in_standalone_mode(client):
    # The default adapter in the standalone fixture has no
    # list_external_users — the route should 501 out.
    resp = await client.get(
        "/api/admin/ha-users",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 501
