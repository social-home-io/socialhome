"""Migration 0094 — ``gfs_connections.proof_required_at``.

Additive: one NULL-default column; existing rows stay unproven (NULL) until
their server first proves possession of its key.

"""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 94


def _apply_through(conn: sqlite3.Connection, last: int) -> None:
    apply_through(conn, last)


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    _apply_through(c, _VERSION - 1)
    yield c
    c.close()


def test_existing_connection_keeps_its_row_and_starts_unproven(conn):
    conn.execute(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name,"
        " public_key, inbox_url, status, paired_at)"
        " VALUES('c1','gfs-1','G','aa','https://gfs.example','active','t')"
    )
    _apply_through(conn, _VERSION)
    row = conn.execute("SELECT * FROM gfs_connections WHERE id='c1'").fetchone()
    assert row["gfs_instance_id"] == "gfs-1"
    assert row["proof_required_at"] is None


def test_column_is_nullable_text(conn):
    _apply_through(conn, _VERSION)
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(gfs_connections)")}
    assert cols["proof_required_at"]["type"].upper() == "TEXT"
    assert cols["proof_required_at"]["notnull"] == 0
    assert cols["proof_required_at"]["dflt_value"] is None
