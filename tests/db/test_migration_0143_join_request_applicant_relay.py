"""Migration 0143 — ``space_join_requests.applicant_relay``: the relay
contact of a household that asked to join through a GFS. One NULL-defaulted
column; existing rows are untouched."""

from __future__ import annotations

import sqlite3

from tests.db.connections import connect
from tests.migration_chain import apply_through

_VERSION = 143


def _fresh(tmp_path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _request(c: sqlite3.Connection, rid: str) -> None:
    c.execute(
        "INSERT OR IGNORE INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-1', 'S', 'inst', 'admin', 'aa')"
    )
    c.execute(
        "INSERT INTO space_join_requests(id, space_id, user_id, expires_at)"
        " VALUES(?, 'sp-1', 'u-1', '2099-01-01T00:00:00+00:00')",
        (rid,),
    )


def test_existing_rows_keep_null(tmp_path):
    c = _fresh(tmp_path)
    apply_through(c, _VERSION - 1)
    _request(c, "r-old")
    apply_through(c, _VERSION)
    row = c.execute(
        "SELECT applicant_relay FROM space_join_requests WHERE id='r-old'"
    ).fetchone()
    assert row["applicant_relay"] is None


def test_column_round_trips(tmp_path):
    c = _fresh(tmp_path)
    apply_through(c, _VERSION)
    _request(c, "r-new")
    c.execute(
        "UPDATE space_join_requests SET applicant_relay=? WHERE id='r-new'",
        ('{"gfs_url": "https://gfs.example"}',),
    )
    row = c.execute(
        "SELECT applicant_relay FROM space_join_requests WHERE id='r-new'"
    ).fetchone()
    assert row["applicant_relay"] == '{"gfs_url": "https://gfs.example"}'
