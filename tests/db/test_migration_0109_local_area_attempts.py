"""Migration 0109 — ``local_area_attempts``, the daily local-community
budgets (one counter per space / kind / subject / UTC day)."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 109

_INSERT = (
    "INSERT INTO local_area_attempts(space_id, subject_kind, subject_id, day,"
    " count) VALUES(?, ?, ?, ?, ?)"
)


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    apply_through(c, _VERSION)
    yield c
    c.close()


@pytest.mark.parametrize("kind", ["person", "household"])
def test_the_two_kinds_are_stored(conn, kind):
    conn.execute(_INSERT, ("s1", kind, "x", "2026-10-19", 1))


@pytest.mark.parametrize("kind", ["space", "verdict_home"])
def test_an_unknown_kind_is_refused(conn, kind):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_INSERT, ("s1", kind, "x", "2026-10-19", 1))


def test_a_negative_count_is_refused(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_INSERT, ("s1", "person", "x", "2026-10-19", -1))


def test_one_row_per_space_kind_subject_and_day(conn):
    conn.execute(_INSERT, ("s1", "person", "x", "2026-10-19", 1))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_INSERT, ("s1", "person", "x", "2026-10-19", 2))
    conn.execute(_INSERT, ("s1", "person", "x", "2026-10-20", 1))
    conn.execute(_INSERT, ("s2", "person", "x", "2026-10-19", 1))
    conn.execute(_INSERT, ("s1", "household", "x", "2026-10-19", 1))
