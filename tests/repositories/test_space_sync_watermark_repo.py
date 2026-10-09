"""Tests for :class:`SqliteSpaceSyncWatermarkRepo` — the §25.6 change-stamp
counter and the per-household watermark on ``space_instances``."""

from __future__ import annotations

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.space import SpaceSyncWatermark
from socialhome.repositories.space_sync_watermark_repo import (
    SqliteSpaceSyncWatermarkRepo,
)


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "wm.db", batch_timeout_ms=1)
    await db.startup()
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp','S','host','anna','ab')"
    )
    await db.enqueue(
        "INSERT INTO space_instances(space_id, instance_id) VALUES('sp','peer')"
    )
    yield db, SqliteSpaceSyncWatermarkRepo(db)
    await db.shutdown()


async def test_current_seq_follows_the_stamping_triggers(env):
    db, repo = env
    assert await repo.current_seq() == 0
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content)"
        " VALUES('p','sp','u','text','x')"
    )
    assert await repo.current_seq() == 1


async def test_no_watermark_until_confirmed(env):
    _db, repo = env
    assert await repo.get("sp", "peer") is None
    assert await repo.get("sp", "stranger") is None


async def test_a_full_confirmation_records_seq_shape_and_full_at(env):
    _db, repo = env
    await repo.confirm("sp", "peer", seq=5, shape="s1", full_at="2026-10-09T00:00:00")
    assert await repo.get("sp", "peer") == SpaceSyncWatermark(
        seq=5, shape="s1", full_at="2026-10-09T00:00:00"
    )


async def test_an_incremental_confirmation_keeps_the_last_full_at(env):
    _db, repo = env
    await repo.confirm("sp", "peer", seq=5, shape="s1", full_at="2026-10-09T00:00:00")
    await repo.confirm("sp", "peer", seq=9, shape="s1", full_at=None)
    assert await repo.get("sp", "peer") == SpaceSyncWatermark(
        seq=9, shape="s1", full_at="2026-10-09T00:00:00"
    )


async def test_confirming_a_household_without_a_seat_records_nothing(env):
    _db, repo = env
    await repo.confirm("sp", "stranger", seq=5, shape="s", full_at="t")
    assert await repo.get("sp", "stranger") is None


async def test_the_watermark_goes_with_the_seat(env):
    db, repo = env
    await repo.confirm("sp", "peer", seq=5, shape="s", full_at="t")
    await db.enqueue(
        "DELETE FROM space_instances WHERE space_id='sp' AND instance_id='peer'"
    )
    await db.enqueue(
        "INSERT INTO space_instances(space_id, instance_id) VALUES('sp','peer')"
    )
    assert await repo.get("sp", "peer") is None
