"""GFS migration 0028 — the binary relay media queue (v_87): a new table
beside ``gfs_envelope_queue``, which is left untouched."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tests.migration_chain import apply_through

_GFS_MIGRATIONS = (
    Path(__file__).resolve().parent.parent.parent
    / "socialhome/global_server/migrations"
)


def _conn(tmp_path) -> sqlite3.Connection:
    c = sqlite3.connect(tmp_path / "gfs.db", isolation_level=None)
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    return c


def test_media_queue_holds_bytes_and_leaves_the_envelope_queue(tmp_path):
    c = _conn(tmp_path)
    apply_through(c, 27, directory=_GFS_MIGRATIONS)
    c.execute(
        "INSERT INTO gfs_envelope_queue(to_instance, sealed_json, created_at,"
        " expires_at) VALUES('x', '{}', 1, 2)"
    )
    apply_through(c, 28, directory=_GFS_MIGRATIONS)
    assert c.execute("SELECT COUNT(*) FROM gfs_envelope_queue").fetchone()[0] == 1
    c.execute(
        "INSERT INTO gfs_media_queue(to_instance, body, size_bytes, created_at,"
        " expires_at) VALUES('x', ?, 3, 1, 2)",
        (b"\x00SH",),
    )
    assert c.execute("SELECT body FROM gfs_media_queue").fetchone()[0] == b"\x00SH"
    c.close()
