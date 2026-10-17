"""Unit tests for the password-reset token repo."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from socialhome.db import AsyncDatabase
from socialhome.repositories.password_reset_repo import (
    SqlitePasswordResetRepo,
    _hash_token,
)


@pytest.fixture
async def db(tmp_path):
    """Real SQLite — runs the schema migration so password_reset_tokens
    + platform_users (FK target) exist. Seeds one platform_users row."""
    sqlite = tmp_path / "test.db"
    db = AsyncDatabase(str(sqlite))
    await db.startup()
    await db.enqueue(
        "INSERT INTO platform_users(username, display_name, is_admin, "
        "password_hash) VALUES('alice','Alice',0,'x')",
        (),
    )
    yield db
    await db.shutdown()


async def test_create_returns_raw_token_and_stores_hash(db):
    repo = SqlitePasswordResetRepo(db)
    raw, expires_at = await repo.create_token("alice", "admin")
    assert isinstance(raw, str) and len(raw) > 32
    row = await db.fetchone(
        "SELECT token, username, expires_at FROM password_reset_tokens",
    )
    # The raw token is NOT in the table — only the SHA-256 hash.
    assert row["token"] == _hash_token(raw)
    assert row["token"] != raw
    assert row["username"] == "alice"
    assert row["expires_at"] == expires_at


async def test_consume_returns_username_and_marks_used(db):
    repo = SqlitePasswordResetRepo(db)
    raw, _ = await repo.create_token("alice", "admin")
    user = await repo.consume_token(raw)
    assert user == "alice"
    row = await db.fetchone(
        "SELECT used_at FROM password_reset_tokens",
    )
    assert row["used_at"] is not None


async def test_consume_unknown_token_returns_none(db):
    repo = SqlitePasswordResetRepo(db)
    user = await repo.consume_token("not-a-token")
    assert user is None


async def test_consume_already_used_returns_none(db):
    repo = SqlitePasswordResetRepo(db)
    raw, _ = await repo.create_token("alice", "admin")
    first = await repo.consume_token(raw)
    second = await repo.consume_token(raw)
    assert first == "alice"
    assert second is None


async def test_consume_expired_returns_none(db):
    repo = SqlitePasswordResetRepo(db)
    past = datetime.now(UTC) - timedelta(hours=2)
    raw, _ = await repo.create_token(
        "alice",
        "admin",
        ttl_seconds=3600,
        now=past,
    )
    user = await repo.consume_token(raw)
    assert user is None


async def test_cleanup_expired_drops_only_expired(db):
    repo = SqlitePasswordResetRepo(db)
    past = datetime.now(UTC) - timedelta(hours=2)
    await repo.create_token("alice", "admin", ttl_seconds=3600, now=past)
    await repo.create_token("alice", "admin")
    n = await repo.cleanup_expired()
    assert n == 1
    rows = await db.fetchall("SELECT 1 FROM password_reset_tokens")
    assert len(rows) == 1
