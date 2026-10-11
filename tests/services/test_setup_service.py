"""Tests for the first-boot SetupService."""

from __future__ import annotations

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.repositories.instance_config_repo import SqliteInstanceConfigRepo
from socialhome.services.setup_service import SETUP_COMPLETE_KEY, SetupService


@pytest.fixture
async def db(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    database = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await database.startup()
    await database.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    yield database
    await database.shutdown()


async def test_is_required_true_on_fresh_db(db):
    svc = SetupService(SqliteInstanceConfigRepo(db))
    assert await svc.is_required() is True


async def test_mark_complete_flips_is_required(db):
    svc = SetupService(SqliteInstanceConfigRepo(db))
    await svc.mark_complete()
    assert await svc.is_required() is False


async def test_mark_complete_is_idempotent(db):
    svc = SetupService(SqliteInstanceConfigRepo(db))
    await svc.mark_complete()
    await svc.mark_complete()
    rows = await db.fetchall(
        "SELECT value FROM instance_config WHERE key=?",
        (SETUP_COMPLETE_KEY,),
    )
    assert [r["value"] for r in rows] == ["1"]


async def test_is_required_treats_blank_value_as_unset(db):
    """A row with an empty value still means the wizard hasn't run."""
    await db.enqueue(
        "INSERT INTO instance_config(key, value) VALUES(?, ?)",
        (SETUP_COMPLETE_KEY, ""),
    )
    svc = SetupService(SqliteInstanceConfigRepo(db))
    assert await svc.is_required() is True
