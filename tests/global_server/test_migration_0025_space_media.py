"""GFS migration 0025 — the space media cache: one metadata row per
encrypted blob, keyed by its SHA-256, with the indexes the expiry sweep, the
per-space quota and the least-recently-fetched eviction read."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.migration_chain import apply_through

_GFS_MIGRATIONS = (
    Path(__file__).resolve().parent.parent.parent
    / "socialhome/global_server/migrations"
)


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "gfs.db", isolation_level=None)
    c.execute("PRAGMA foreign_keys=ON")
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    apply_through(c, 25, directory=_GFS_MIGRATIONS)
    yield c
    c.close()


_INS = (
    "INSERT INTO gfs_space_media(blob_id, scope_kind, scope_id, size,"
    " created_at, expires_at, last_fetched_at)"
    " VALUES(?, 'space', 's1', ?, 10, 20, 10)"
)


def test_one_row_per_blob(conn):
    conn.execute(_INS, ("a" * 64, 65_564))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_INS, ("a" * 64, 65_564))


def test_size_must_be_positive(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_INS, ("b" * 64, 0))


def test_the_sweep_quota_and_eviction_indexes_exist(conn):
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name='gfs_space_media'"
        )
    }
    assert {
        "idx_gfs_space_media_expires",
        "idx_gfs_space_media_scope",
        "idx_gfs_space_media_lru",
    } <= names


def test_the_lru_scan_uses_its_index(conn):
    plan = " ".join(
        str(r[-1])
        for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT blob_id FROM gfs_space_media"
            " ORDER BY last_fetched_at, created_at LIMIT 10"
        )
    )
    assert "idx_gfs_space_media_lru" in plan


def test_the_scope_kind_is_a_space_or_a_channel(conn):
    conn.execute(
        "INSERT INTO gfs_space_media(blob_id, scope_kind, scope_id, size,"
        " created_at, expires_at, last_fetched_at)"
        " VALUES(?, 'channel', ?, 1, 1, 2, 1)",
        ("c" * 64, "0f" * 16),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO gfs_space_media(blob_id, scope_kind, scope_id, size,"
            " created_at, expires_at, last_fetched_at)"
            " VALUES(?, 'room', 'x', 1, 1, 2, 1)",
            ("d" * 64,),
        )


def test_the_per_scope_eviction_scan_uses_its_index(conn):
    plan = " ".join(
        str(r[-1])
        for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT blob_id FROM gfs_space_media"
            " WHERE scope_kind='space' AND scope_id='s1'"
            " ORDER BY last_fetched_at, created_at LIMIT 10"
        )
    )
    assert "idx_gfs_space_media_scope" in plan
