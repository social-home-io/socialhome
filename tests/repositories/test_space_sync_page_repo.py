"""Tests for :class:`SqliteSpaceSyncPageRepo` — the newest sync page per
space (migration 0151)."""

from __future__ import annotations

import sqlite3

import orjson
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
KEY = "ab"


def _row(
    seq: int, *, issued_at: int = 1_000, epoch: int = 0, vector=VECTOR, signer="host"
):
    """A stored row whose header carries the order key ``(epoch, issued_at,
    seq)`` the repo compares and its ``signer`` (the rest of a header plays
    no part here)."""
    page_json = orjson.dumps(
        {"epoch": epoch, "issued_at": issued_at, "seq": seq, "signer": signer}
    ).decode()
    return StoredSyncPage("sp", page_json, vector)


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "pages.db", batch_timeout_ms=1)
    await db.startup()
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp','S','host','anna',?)",
        (KEY,),
    )
    yield db, SqliteSpaceSyncPageRepo(db)
    await db.shutdown()


async def test_put_get_replace(env):
    _db, repo = env
    assert isinstance(repo, AbstractSpaceSyncPageRepo)
    assert await repo.get("sp") is None
    first = _row(1)
    assert await repo.put(first, key=KEY)
    assert await repo.get("sp") == first
    second = _row(2, vector=b"\x02" * 8192)
    assert await repo.put(second, key=KEY)
    assert await repo.get("sp") == second


async def test_only_a_newer_page_replaces_the_one_held(env):
    """Final review minor 6: the order key ``(epoch, issued_at, seq)`` is
    checked in the same statement that writes — a slower writer holding an
    older page never overwrites a newer one stored meanwhile."""
    _db, repo = env
    newer = _row(5, issued_at=2_000)
    assert await repo.put(newer, key=KEY)
    assert not await repo.put(_row(9, issued_at=1_999), key=KEY)
    assert not await repo.put(_row(4, issued_at=2_000), key=KEY)
    assert not await repo.put(_row(5, issued_at=2_000, vector=b"\x03" * 8192), key=KEY)
    assert await repo.get("sp") == newer
    later = _row(1, issued_at=2_001)
    assert await repo.put(later, key=KEY)
    assert await repo.get("sp") == later


async def test_a_page_verified_under_another_key_or_epoch_is_refused(env):
    """Final review minor 6: a page verified against a key or epoch the
    space no longer pins (a re-pin or rotation between the verify and the
    write) is never stored."""
    db, repo = env
    assert not await repo.put(_row(1), key="cd")
    assert not await repo.put(_row(1, epoch=1), key=KEY)
    assert await repo.get("sp") is None
    await db.enqueue("UPDATE spaces SET authority_key_epoch=1 WHERE id='sp'")
    assert await repo.put(_row(1, epoch=1), key=KEY.upper())


async def test_a_page_for_a_space_not_held_is_not_stored(env):
    _db, repo = env
    gone = StoredSyncPage("sp-gone", _row(1).page_json, VECTOR)
    assert not await repo.put(gone, key=KEY)
    assert await repo.get("sp-gone") is None


async def test_the_page_goes_with_the_space(env):
    db, repo = env
    await repo.put(_row(1), key=KEY)
    await db.enqueue("DELETE FROM spaces WHERE id='sp'")
    assert await repo.get("sp") is None


async def test_an_authority_rotation_drops_the_page(env):
    """§3: an old-epoch page is stale — it goes with the epoch move, on the
    owner's and on a member's path alike (trigger)."""
    db, repo = env
    await repo.put(_row(1), key=KEY)
    await db.enqueue("UPDATE spaces SET name='renamed' WHERE id='sp'")
    assert await repo.get("sp") is not None
    await db.enqueue(
        "UPDATE spaces SET authority_key_epoch=authority_key_epoch+1 WHERE id='sp'"
    )
    assert await repo.get("sp") is None
    # A re-pin that keeps the epoch drops it too.
    await repo.put(_row(1, epoch=1), key=KEY)
    assert await repo.get("sp") is not None
    await db.enqueue("UPDATE spaces SET identity_public_key='cd' WHERE id='sp'")
    assert await repo.get("sp") is None


async def test_storage_stays_within_the_documented_bound(env):
    """``docs/protocol/sync-pages.md``: ≈ 9 KB per space, on the signer and
    on a member alike (no delta since the final review, I4)."""
    db, repo = env
    page = sign_page(
        space_id="sp",
        epoch=0,
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
    assert await repo.put(StoredSyncPage("sp", page_json, VECTOR), key=KEY)
    row = await db.fetchone(
        "SELECT length(page_json) + length(vector) AS n FROM space_sync_pages"
    )
    assert row["n"] <= 9 * 1024


async def test_the_schema_refuses_a_wrong_sized_vector_and_has_no_delta(env):
    db, _repo = env
    with pytest.raises(sqlite3.IntegrityError):
        await db.enqueue(
            "INSERT INTO space_sync_pages(space_id, page_json, vector)"
            " VALUES('sp','{}',?)",
            (b"\x00" * 100,),
        )
    cols = {r["name"] for r in await db.fetchall("PRAGMA table_info(space_sync_pages)")}
    assert cols == {"space_id", "signer", "page_json", "vector"}


async def test_each_signer_keeps_its_own_newest_page(env):
    """Security re-check: one row per (space, signer). The owner's page and
    a delegated admin's never replace each other; ``get`` is the newest
    across signers, ``list_all`` every signer's newest, newest first; a
    rotation drops them all; a page naming no signer is never stored."""
    db, repo = env
    owner = _row(5, issued_at=3_000, signer="host")
    admin = _row(9, issued_at=2_000, signer="admin-1")
    assert await repo.put(owner, key=KEY)
    assert await repo.put(admin, key=KEY)
    assert await repo.get("sp") == owner
    assert await repo.list_all("sp") == [owner, admin]
    older_admin = _row(8, issued_at=1_500, signer="admin-1")
    assert not await repo.put(older_admin, key=KEY)  # not newer FOR THAT SIGNER
    newer_admin = _row(10, issued_at=4_000, signer="admin-1")
    assert await repo.put(newer_admin, key=KEY)
    assert await repo.list_all("sp") == [newer_admin, owner]
    nameless = StoredSyncPage(
        "sp", orjson.dumps({"epoch": 0, "issued_at": 9, "seq": 1}).decode(), VECTOR
    )
    assert not await repo.put(nameless, key=KEY)
    await db.enqueue(
        "UPDATE spaces SET authority_key_epoch=authority_key_epoch+1 WHERE id='sp'"
    )
    assert await repo.list_all("sp") == []
