"""HTTP tests for /api/backup/* — admin-only export + restore.

Backup routes only mount when ``config.mode == "ha"``. This file
provides its own ``ha_client`` fixture; the standalone ``client``
fixture from conftest.py would yield 404s for these routes.

Also tests the HA-only mounting guard via the standalone client.
"""

from __future__ import annotations

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key as _db_key
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.crypto import derive_user_id

from .conftest import _auth


# ─── HA-mode client fixture ──────────────────────────────────────────────


@pytest.fixture
async def ha_client(aiohttp_client, tmp_dir):
    """Same shape as the standalone ``client`` but with mode='ha'."""
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="ha",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
    )
    app = create_app(cfg)
    tc = await aiohttp_client(app)

    db = app[_db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'"
    )
    pk_bytes = bytes.fromhex(row["identity_public_key"])
    uid = derive_user_id(pk_bytes, "admin")
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,1)",
        ("admin", uid, "Admin"),
    )
    raw = "test-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t1", uid, "t", sha256_token_hash(raw)),
    )
    tc._tok = raw
    tc._uid = uid
    tc._db = db
    return tc


# ─── Backup routes are adapter-agnostic (always mounted) ────────────────


async def test_backup_export_returns_200_in_standalone(client):
    """Backup routes are mounted for all adapters — standalone included."""
    r = await client.get("/api/backup/export", headers=_auth(client._tok))
    assert r.status == 200


async def test_pre_backup_returns_200_in_standalone(client):
    r = await client.post("/api/backup/pre_backup", headers=_auth(client._tok))
    assert r.status == 200


# ─── pre_backup / post_backup ────────────────────────────────────────────


async def test_pre_backup_admin_returns_checkpoint_stats(ha_client):
    r = await ha_client.post(
        "/api/backup/pre_backup",
        headers=_auth(ha_client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True
    # PRAGMA wal_checkpoint returns three integers; check shape.
    assert "busy" in body
    assert "log_frames" in body
    assert "checkpointed_frames" in body
    assert isinstance(body["log_frames"], int)


async def test_pre_backup_idempotent(ha_client):
    """Calling twice in a row must succeed both times."""
    for _ in range(3):
        r = await ha_client.post(
            "/api/backup/pre_backup",
            headers=_auth(ha_client._tok),
        )
        assert r.status == 200


async def test_pre_backup_non_admin_403(ha_client):
    db = ha_client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES('bob', 'bob-id', 'Bob', 0)",
    )
    raw = "bob-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES('tb', 'bob-id', 't', ?)",
        (sha256_token_hash(raw),),
    )
    r = await ha_client.post("/api/backup/pre_backup", headers=_auth(raw))
    assert r.status == 403


async def test_post_backup_returns_ack(ha_client):
    r = await ha_client.post(
        "/api/backup/post_backup",
        headers=_auth(ha_client._tok),
    )
    assert r.status == 200
    assert (await r.json())["ok"] is True


async def test_post_backup_non_admin_403(ha_client):
    db = ha_client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES('eve', 'eve-id', 'Eve', 0)",
    )
    raw = "eve-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES('te', 'eve-id', 't', ?)",
        (sha256_token_hash(raw),),
    )
    r = await ha_client.post("/api/backup/post_backup", headers=_auth(raw))
    assert r.status == 403


# ─── Export ──────────────────────────────────────────────────────────────


async def test_export_admin_returns_gzip(ha_client):
    r = await ha_client.get("/api/backup/export", headers=_auth(ha_client._tok))
    assert r.status == 200
    assert r.content_type == "application/gzip"
    body = await r.read()
    assert body.startswith(b"\x1f\x8b")


async def test_export_unauth_401_or_403(ha_client):
    """Unauthenticated → 401 from middleware, before route."""
    r = await ha_client.get("/api/backup/export")
    assert r.status in (401, 403)


async def test_export_non_admin_403(ha_client):
    db = ha_client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES('bob3', 'bob3-id', 'Bob', 0)",
    )
    raw = "bob3-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES('tb3', 'bob3-id', 't', ?)",
        (sha256_token_hash(raw),),
    )
    r = await ha_client.get("/api/backup/export", headers=_auth(raw))
    assert r.status == 403


# ─── Import ──────────────────────────────────────────────────────────────


async def test_import_empty_body_422(ha_client):
    r = await ha_client.post(
        "/api/backup/import",
        data=b"",
        headers=_auth(ha_client._tok),
    )
    assert r.status == 422


async def test_import_existing_db_409(ha_client):
    """Round-trip: export → import → 409 because users exist."""
    r = await ha_client.get("/api/backup/export", headers=_auth(ha_client._tok))
    blob = await r.read()
    r = await ha_client.post(
        "/api/backup/import",
        data=blob,
        headers={**_auth(ha_client._tok), "Content-Type": "application/gzip"},
    )
    assert r.status == 409


async def test_import_non_admin_403(ha_client):
    db = ha_client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES('bob4', 'bob4-id', 'Bob', 0)",
    )
    raw = "bob4-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES('tb4', 'bob4-id', 't', ?)",
        (sha256_token_hash(raw),),
    )
    r = await ha_client.post(
        "/api/backup/import",
        data=b"x",
        headers=_auth(raw),
    )
    assert r.status == 403
