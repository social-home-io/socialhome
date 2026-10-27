"""Migration 0128 — ``highlight_stamps`` (v_70): a new table only, every
existing highlight untouched; a stamp holds at least one of its two times and
outlives the highlight it stamps (no FK)."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 128


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    apply_through(c, _VERSION - 1)
    c.execute(
        "INSERT INTO highlights(id, author_user_id, highlight_date, expires_at)"
        " VALUES('h1', 'u-a', '2026-10-26', datetime('now', '+1 day'))"
    )
    before = [tuple(r) for r in c.execute("SELECT * FROM highlights")]
    apply_through(c, _VERSION)
    assert [tuple(r) for r in c.execute("SELECT * FROM highlights")] == before
    yield c
    c.close()


def test_a_stamp_needs_one_of_its_times(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO highlight_stamps(highlight_id, author_user_id, expires_at)"
            " VALUES('h1', 'u-a', '2026-11-01')"
        )


def test_a_stamp_outlives_the_highlight(conn):
    conn.execute(
        "INSERT INTO highlight_stamps(highlight_id, author_user_id, removed_at,"
        " expires_at) VALUES('h1', 'u-a', '2026-10-26T10:00:00+00:00',"
        " '2026-11-01')"
    )
    conn.execute("DELETE FROM highlights WHERE id='h1'")
    row = conn.execute(
        "SELECT removed_at FROM highlight_stamps WHERE highlight_id='h1'"
    ).fetchone()
    assert row[0] == "2026-10-26T10:00:00+00:00"
