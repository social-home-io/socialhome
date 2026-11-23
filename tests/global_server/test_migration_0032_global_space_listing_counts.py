"""GFS migration 0032 — a global space's declared member + follower counts:
three nullable columns on ``global_spaces``; existing rows keep NULL ("no
number"), never ``0``."""

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


def test_an_existing_listing_keeps_null_counts(tmp_path):
    c = _conn(tmp_path)
    apply_through(c, 31, directory=_GFS_MIGRATIONS)
    c.execute(
        "INSERT INTO global_spaces(space_id, owning_instance, name,"
        " subscriber_count) VALUES('sp-1', 'owner', 'S', 4)"
    )
    apply_through(c, 32, directory=_GFS_MIGRATIONS)
    row = c.execute(
        "SELECT member_count, follower_count, counts_ts, subscriber_count"
        " FROM global_spaces"
    ).fetchone()
    # No backfill: the relay-seat count is NOT a member count.
    assert row == (None, None, None, 4)
    c.execute(
        "UPDATE global_spaces SET member_count=3, follower_count=0,"
        " counts_ts='2026-11-23T10:00:00+00:00'"
    )
    assert c.execute(
        "SELECT member_count, follower_count, counts_ts FROM global_spaces"
    ).fetchone() == (3, 0, "2026-11-23T10:00:00+00:00")
    c.close()
