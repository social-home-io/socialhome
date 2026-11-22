"""Tests for :class:`SqliteSpaceSyncWatermarkRepo` — the §25.6 change-stamp
counter and the per-household watermark on ``space_instances``."""

from __future__ import annotations

import dataclasses
import sqlite3

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.space import SpaceSyncWatermark
from socialhome.domain.space_sync_status import (
    SpaceSyncOutcome,
    SpaceSyncResult,
    parse_utc,
)
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


# ── The requester's echo (migration 0087) ─────────────────────────────────


async def test_no_applied_seq_until_a_clean_stream_is_recorded(env):
    _db, repo = env
    assert await repo.applied_seq("sp", "peer") is None
    assert await repo.applied_seq("sp", "stranger") is None


async def test_record_applied_keeps_the_latest_and_leaves_the_watermark(env):
    _db, repo = env
    await repo.confirm("sp", "peer", seq=5, shape="s", full_at="t")
    await repo.record_applied("sp", "peer", 11)
    await repo.record_applied("sp", "peer", 12)
    assert await repo.applied_seq("sp", "peer") == 12
    assert (await repo.get("sp", "peer")).seq == 5


async def test_record_applied_for_a_household_without_a_seat_is_a_noop(env):
    _db, repo = env
    await repo.record_applied("sp", "stranger", 3)
    assert await repo.applied_seq("sp", "stranger") is None


async def test_the_applied_seq_goes_with_the_seat(env):
    db, repo = env
    await repo.record_applied("sp", "peer", 4)
    await db.enqueue(
        "DELETE FROM space_instances WHERE space_id='sp' AND instance_id='peer'"
    )
    await db.enqueue(
        "INSERT INTO space_instances(space_id, instance_id) VALUES('sp','peer')"
    )
    assert await repo.applied_seq("sp", "peer") is None


@pytest.mark.parametrize("seq", [2**63, -1])
async def test_record_applied_refuses_a_value_sqlite_cannot_hold(env, seq):
    """Refused before it reaches the writer: an out-of-range int raises
    ``OverflowError`` inside the coalesced batch and fails every write in it."""
    db, repo = env
    await repo.record_applied("sp", "peer", 5)
    with pytest.raises(ValueError):
        await repo.record_applied("sp", "peer", seq)
    await db.enqueue(
        "INSERT INTO space_instances(space_id, instance_id) VALUES('sp','other')"
    )
    assert await repo.applied_seq("sp", "peer") == 5
    assert "other" in {
        r["instance_id"]
        for r in await db.fetchall("SELECT instance_id FROM space_instances")
    }


# ── The last sync outcome per acting host (migration 0150) ────────────────


def _no_join(outcomes):
    """The outcomes without their seat's join time (asserted on its own)."""
    return [dataclasses.replace(o, joined_at=None) for o in outcomes]


async def test_an_outcome_carries_when_the_seat_was_written(env):
    """No ``ok`` yet: "may be missing" counts failed rounds from the join."""
    _db, repo = env
    (outcome,) = await repo.outcomes("sp", ["peer"])
    assert outcome.joined_at  # SQLite's datetime('now') default
    assert parse_utc(outcome.joined_at) is not None


async def test_no_outcome_until_one_is_recorded(env):
    _db, repo = env
    assert _no_join(await repo.outcomes("sp", ["peer", "stranger"])) == [
        SpaceSyncOutcome(instance_id="peer", result=None, ok_at=None, result_at=None)
    ]


async def test_an_ok_records_the_time_and_a_later_refusal_keeps_it(env):
    _db, repo = env
    await repo.record_outcome("sp", "peer", SpaceSyncResult.OK, at="t1", ok_at="t1")
    await repo.record_outcome("sp", "peer", SpaceSyncResult.DEFERRED, at="t2")
    assert _no_join(await repo.outcomes("sp", ["peer"])) == [
        SpaceSyncOutcome(
            instance_id="peer",
            result=SpaceSyncResult.DEFERRED,
            ok_at="t1",
            result_at="t2",
        )
    ]


async def test_an_outcome_for_a_household_without_a_seat_records_nothing(env):
    _db, repo = env
    await repo.record_outcome("sp", "stranger", SpaceSyncResult.OK, at="t1", ok_at="t1")
    assert await repo.outcomes("sp", ["stranger"]) == []


async def test_the_outcome_survives_a_restart(tmp_dir):
    path = tmp_dir / "restart.db"
    db = AsyncDatabase(path, batch_timeout_ms=1)
    await db.startup()
    await db.enqueue(
        "INSERT INTO space_instances(space_id, instance_id) VALUES('sp','host')"
    )
    await SqliteSpaceSyncWatermarkRepo(db).record_outcome(
        "sp", "host", SpaceSyncResult.UNREACHABLE, at="t1"
    )
    await db.shutdown()
    db = AsyncDatabase(path, batch_timeout_ms=1)
    await db.startup()
    try:
        assert _no_join(
            await SqliteSpaceSyncWatermarkRepo(db).outcomes("sp", ["host"])
        ) == [
            SpaceSyncOutcome(
                instance_id="host",
                result=SpaceSyncResult.UNREACHABLE,
                ok_at=None,
                result_at="t1",
            )
        ]
    finally:
        await db.shutdown()


async def test_the_database_refuses_an_unknown_result(env):
    db, _repo = env
    with pytest.raises(sqlite3.IntegrityError):
        await db.enqueue(
            "UPDATE space_instances SET sync_result='maybe' WHERE instance_id='peer'"
        )
