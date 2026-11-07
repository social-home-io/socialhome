"""GFS migration 0027 — a global space's join contact (v_84): one nullable
JSON column on ``global_spaces``; existing rows keep NULL."""

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


def test_an_existing_listing_keeps_null(tmp_path):
    c = _conn(tmp_path)
    apply_through(c, 26, directory=_GFS_MIGRATIONS)
    c.execute(
        "INSERT INTO global_spaces(space_id, owning_instance, name)"
        " VALUES('sp-1', 'owner', 'S')"
    )
    apply_through(c, 27, directory=_GFS_MIGRATIONS)
    assert c.execute("SELECT join_contact FROM global_spaces").fetchone()[0] is None
    c.execute("UPDATE global_spaces SET join_contact='{\"a\": 1}'")
    assert c.execute("SELECT join_contact FROM global_spaces").fetchone()[0] == (
        '{"a": 1}'
    )
    c.close()
