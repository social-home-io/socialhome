"""Tests for SqliteSpaceCoverRepo (per-space cover blob)."""

from __future__ import annotations

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.repositories.space_cover_repo import SqliteSpaceCoverRepo


@pytest.fixture
async def repo(tmp_dir):
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
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-1', 'X', ?, 'p', ?)",
        (iid, "ab" * 32),
    )
    yield SqliteSpaceCoverRepo(db)
    await db.shutdown()


async def test_get_returns_none_when_unset(repo):
    assert await repo.get("sp-1") is None


async def test_set_get_clear_roundtrip(repo):
    await repo.set("sp-1", bytes_webp=b"RIFFwebp", hash="abc", width=1200, height=800)
    assert await repo.get("sp-1") == (b"RIFFwebp", "abc")
    await repo.clear("sp-1")
    assert await repo.get("sp-1") is None


async def test_replace_bytes_if_hash_writes_only_over_that_version(repo):
    """A rendition replaces the stored image only while it is still the
    version it was made from — a set or clear meanwhile is never undone."""
    await repo.set("sp-1", bytes_webp=b"RIFFold", hash="h1", width=1, height=1)
    assert await repo.replace_bytes_if_hash("sp-1", bytes_webp=b"RIFFsmall", hash="h1")
    assert await repo.get("sp-1") == (b"RIFFsmall", "h1")
    await repo.set("sp-1", bytes_webp=b"RIFFnew", hash="h2", width=1, height=1)
    assert not await repo.replace_bytes_if_hash("sp-1", bytes_webp=b"RIFFx", hash="h1")
    assert await repo.get("sp-1") == (b"RIFFnew", "h2")
    await repo.clear("sp-1")
    assert not await repo.replace_bytes_if_hash("sp-1", bytes_webp=b"RIFFx", hash="h2")
    assert await repo.get("sp-1") is None
