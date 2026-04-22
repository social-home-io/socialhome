"""Shared fixtures for GFS tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from socialhome.db.database import AsyncDatabase

_GFS_MIGRATIONS = Path(__file__).resolve().parent.parent.parent / (
    "socialhome/global_server/migrations"
)


@pytest.fixture
async def gfs_db(tmp_dir):
    """AsyncDatabase pointed at a temp GFS database with migrations applied."""
    db = AsyncDatabase(
        tmp_dir / "gfs.db",
        migrations_dir=_GFS_MIGRATIONS,
        batch_timeout_ms=10,
    )
    await db.startup()
    yield db
    await db.shutdown()
