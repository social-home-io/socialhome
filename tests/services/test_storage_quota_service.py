"""Tests for StorageQuotaService."""

from __future__ import annotations

import json

import pytest

from social_home.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from social_home.db.database import AsyncDatabase
from social_home.repositories.storage_stats_repo import SqliteStorageStatsRepo
from social_home.services.storage_quota_service import (
    StorageQuotaExceeded,
    StorageQuotaService,
)


def _svc(db, *, quota_bytes: int) -> StorageQuotaService:
    """Test helper — builds the service against the SQLite repo."""
    return StorageQuotaService(SqliteStorageStatsRepo(db), quota_bytes=quota_bytes)


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES('u1', 'u1-id', 'U1')",
    )
    yield db
    await db.shutdown()


async def _seed_post(db, post_id: str, size: int) -> None:
    meta = json.dumps(
        {
            "url": f"/m/{post_id}",
            "mime_type": "application/octet-stream",
            "original_name": "x.bin",
            "size_bytes": size,
        }
    )
    await db.enqueue(
        "INSERT INTO feed_posts(id, author, type, content, file_meta_json)"
        " VALUES(?, 'u1-id', 'file', '', ?)",
        (post_id, meta),
    )


# ─── current_usage_bytes ─────────────────────────────────────────────────


async def test_zero_usage_when_no_files(env):
    svc = _svc(env, quota_bytes=1024)
    assert await svc.current_usage_bytes() == 0


async def test_sums_feed_post_file_meta(env):
    await _seed_post(env, "p1", 100)
    await _seed_post(env, "p2", 250)
    svc = _svc(env, quota_bytes=10_000)
    assert await svc.current_usage_bytes() == 350


async def test_skips_malformed_file_meta(env):
    await env.enqueue(
        "INSERT INTO feed_posts(id, author, type, content, file_meta_json)"
        " VALUES('bad', 'u1-id', 'file', '', 'not-json')",
    )
    svc = _svc(env, quota_bytes=10_000)
    assert await svc.current_usage_bytes() == 0


async def test_skips_meta_without_size(env):
    await env.enqueue(
        "INSERT INTO feed_posts(id, author, type, content, file_meta_json)"
        " VALUES('nosz', 'u1-id', 'file', '', '{\"url\":\"x\"}')",
    )
    svc = _svc(env, quota_bytes=10_000)
    assert await svc.current_usage_bytes() == 0


# ─── usage ───────────────────────────────────────────────────────────────


async def test_usage_returns_struct(env):
    await _seed_post(env, "p1", 200)
    svc = _svc(env, quota_bytes=1000)
    u = await svc.usage()
    assert u.used_bytes == 200
    assert u.quota_bytes == 1000
    assert u.available_bytes == 800
    assert u.percent_used == 20.0


async def test_usage_percent_zero_when_quota_zero(env):
    svc = _svc(env, quota_bytes=0)
    u = await svc.usage()
    assert u.percent_used == 0.0


# ─── check_can_store ─────────────────────────────────────────────────────


async def test_check_can_store_passes_when_under_quota(env):
    await _seed_post(env, "p1", 100)
    svc = _svc(env, quota_bytes=1000)
    await svc.check_can_store(500)  # 100 + 500 = 600 < 1000


async def test_check_can_store_raises_when_over_quota(env):
    await _seed_post(env, "p1", 800)
    svc = _svc(env, quota_bytes=1000)
    with pytest.raises(StorageQuotaExceeded) as exc:
        await svc.check_can_store(500)
    assert exc.value.requested == 500
    assert exc.value.available == 200


async def test_check_can_store_disabled_when_quota_zero(env):
    svc = _svc(env, quota_bytes=0)
    # Enormous request must NOT raise.
    await svc.check_can_store(10_000_000_000)


async def test_check_can_store_ignores_zero_or_negative(env):
    svc = _svc(env, quota_bytes=10)
    await svc.check_can_store(0)
    await svc.check_can_store(-5)
