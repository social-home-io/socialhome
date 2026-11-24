"""Migration 0156 — ``gfs_space_seats`` (0092), the identity-bound record of
this household's legacy ``/gfs/subscribe`` seats, is dropped; nothing else
changes."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 156


def _tables(c: sqlite3.Connection) -> set[str]:
    return {
        r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


@pytest.fixture
def db(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    apply_through(c, _VERSION - 1)
    c.execute(
        "INSERT INTO gfs_space_seats(space_id, gfs_instance_id) VALUES('s1', 'gfs-1')"
    )
    yield c
    c.close()


def test_the_seat_table_and_its_rows_are_gone(db):
    before = _tables(db)
    assert "gfs_space_seats" in before
    apply_through(db, _VERSION)
    assert _tables(db) == before - {"gfs_space_seats"}
    assert (
        db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE sql LIKE '%gfs_space_seats%'"
        ).fetchone()[0]
        == 0
    )
