"""Tests for :class:`SqliteSpaceSyncPageRepo` — the newest sync page per
space (migration 0151)."""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.canonical_json import canonical_str
from socialhome.db.database import AsyncDatabase
from socialhome.domain.space_sync_page import StoredSyncPage
from socialhome.repositories.space_sync_page_repo import (
    AbstractSpaceSyncPageRepo,
    SqliteSpaceSyncPageRepo,
)
from socialhome.sync_page import sign_page

VECTOR = b"\x01" * 8192


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "pages.db", batch_timeout_ms=1)
    await db.startup()
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp','S','host','anna','ab')"
    )
    yield db, SqliteSpaceSyncPageRepo(db)
    await db.shutdown()


async def test_put_get_replace_delete(env):
    _db, repo = env
    assert isinstance(repo, AbstractSpaceSyncPageRepo)
    assert await repo.get("sp") is None
    first = StoredSyncPage("sp", '{"seq":1}', VECTOR)
    await repo.put(first)
    assert await repo.get("sp") == first
    second = StoredSyncPage("sp", '{"seq":2}', b"\x02" * 8192, delta=b"\x07" * 33)
    await repo.put(second)
    assert await repo.get("sp") == second
    await repo.delete("sp")
    assert await repo.get("sp") is None


async def test_a_page_for_a_space_not_held_is_not_stored(env):
    _db, repo = env
    await repo.put(StoredSyncPage("sp-gone", "{}", VECTOR))
    assert await repo.get("sp-gone") is None


async def test_the_page_goes_with_the_space(env):
    db, repo = env
    await repo.put(StoredSyncPage("sp", "{}", VECTOR))
    await db.enqueue("DELETE FROM spaces WHERE id='sp'")
    assert await repo.get("sp") is None


async def test_an_authority_rotation_drops_the_page(env):
    """§3: an old-epoch page is stale — it goes with the epoch move, on the
    owner's and on a member's path alike (trigger)."""
    db, repo = env
    await repo.put(StoredSyncPage("sp", "{}", VECTOR))
    await db.enqueue("UPDATE spaces SET name='renamed' WHERE id='sp'")
    assert await repo.get("sp") is not None
    await db.enqueue(
        "UPDATE spaces SET authority_key_epoch=authority_key_epoch+1 WHERE id='sp'"
    )
    assert await repo.get("sp") is None


async def test_storage_stays_within_the_documented_bound(env):
    """``docs/protocol/sync-pages.md``: ≤ 17 KB per space on a signer
    (a full-size page + vector + a full 256-bucket delta), ≈ 9 KB on a
    member (no delta)."""
    db, repo = env
    page = sign_page(
        space_id="sp",
        epoch=2**31,
        signer="i" * 64,
        seq=2**40,
        issued_at=1_800_000_000_000,
        cutoff="2026-11-21 12:00:00",
        rows=1_000_000,
        vector=VECTOR,
        prev_root=b"\x05" * 32,
        space_seed=bytes(32),
    )
    page_json = canonical_str(page.to_wire())
    assert len(page_json) < 600
    await repo.put(StoredSyncPage("sp", page_json, VECTOR, delta=b"\x00" * 33 * 256))
    row = await db.fetchone(
        "SELECT length(page_json) + length(vector) + length(delta) AS n"
        " FROM space_sync_pages"
    )
    assert row["n"] <= 17 * 1024
    await repo.put(StoredSyncPage("sp", page_json, VECTOR))
    row = await db.fetchone(
        "SELECT length(page_json) + length(vector) AS n FROM space_sync_pages"
    )
    assert row["n"] <= 9 * 1024


async def test_the_schema_refuses_a_wrong_sized_vector_or_delta(env):
    db, _repo = env
    with pytest.raises(sqlite3.IntegrityError):
        await db.enqueue(
            "INSERT INTO space_sync_pages(space_id, page_json, vector)"
            " VALUES('sp','{}',?)",
            (b"\x00" * 100,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        await db.enqueue(
            "INSERT INTO space_sync_pages(space_id, page_json, vector, delta)"
            " VALUES('sp','{}',?,?)",
            (VECTOR, b"\x00" * 34),
        )
