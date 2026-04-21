"""Tests for /api/reports + /api/admin/reports."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from social_home.app import create_app
from social_home.app_keys import db_key as _db_key
from social_home.auth import sha256_token_hash
from social_home.config import Config
from social_home.crypto import derive_user_id


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


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
            "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,1)",
            ("pascal", admin_uid, "Pascal"),
        )
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
            ("bob", bob_uid, "Bob"),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-admin", admin_uid, "t", sha256_token_hash("admin-token")),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-bob", bob_uid, "t", sha256_token_hash("bob-token")),
        )
        tc._admin_token = "admin-token"
        tc._admin_uid = admin_uid
        tc._bob_token = "bob-token"
        tc._bob_uid = bob_uid
        yield tc


async def test_create_report_201(client):
    resp = await client.post(
        "/api/reports",
        json={"target_type": "post", "target_id": "p-1", "category": "spam"},
        headers=_auth(client._bob_token),
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["status"] == "pending"
    # ``federated`` is always returned; false for a target with no hosting peer.
    assert body["federated"] is False


async def test_create_report_missing_fields_422(client):
    resp = await client.post(
        "/api/reports",
        json={"target_type": "post"},
        headers=_auth(client._bob_token),
    )
    assert resp.status == 422


async def test_duplicate_report_409(client):
    payload = {"target_type": "post", "target_id": "p-1", "category": "spam"}
    r = await client.post(
        "/api/reports", json=payload, headers=_auth(client._bob_token)
    )
    assert r.status == 201
    r2 = await client.post(
        "/api/reports", json=payload, headers=_auth(client._bob_token)
    )
    assert r2.status == 409


async def test_admin_list_reports(client):
    await client.post(
        "/api/reports",
        json={"target_type": "post", "target_id": "p-1", "category": "spam"},
        headers=_auth(client._bob_token),
    )
    resp = await client.get(
        "/api/admin/reports",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert len(body) == 1
    assert body[0]["status"] == "pending"


async def test_admin_list_forbidden_for_non_admin(client):
    resp = await client.get(
        "/api/admin/reports",
        headers=_auth(client._bob_token),
    )
    assert resp.status == 403


async def test_admin_resolve_report(client):
    r = await client.post(
        "/api/reports",
        json={"target_type": "post", "target_id": "p-1", "category": "spam"},
        headers=_auth(client._bob_token),
    )
    report_id = (await r.json())["id"]
    resp = await client.post(
        f"/api/admin/reports/{report_id}/resolve",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    listed = await (
        await client.get(
            "/api/admin/reports",
            headers=_auth(client._admin_token),
        )
    ).json()
    assert listed == []


async def test_admin_resolve_twice_409(client):
    r = await client.post(
        "/api/reports",
        json={"target_type": "post", "target_id": "p-1", "category": "spam"},
        headers=_auth(client._bob_token),
    )
    report_id = (await r.json())["id"]
    await client.post(
        f"/api/admin/reports/{report_id}/resolve",
        headers=_auth(client._admin_token),
    )
    resp = await client.post(
        f"/api/admin/reports/{report_id}/resolve",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 409
