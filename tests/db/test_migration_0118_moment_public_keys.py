"""Migration 0118 — moment keys (v_62): one row per author, role and
generation; an own key and a delivered key never share a row."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 118


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
    apply_through(c, _VERSION)
    yield c
    c.close()


def test_one_row_per_author_role_and_generation(conn):
    ins = (
        "INSERT INTO moment_public_keys(author_user_id, is_own, key_gen, key_sealed)"
        " VALUES(?, ?, ?, ?)"
    )
    conn.execute(ins, ("alice", 1, 100, "k"))
    conn.execute(ins, ("alice", 0, 100, "k"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("alice", 1, 100, "other"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("alice", 2, 101, "k"))
    row = conn.execute(
        "SELECT created_at, key_step FROM moment_public_keys WHERE is_own=1"
    ).fetchone()
    assert row["created_at"] and row["key_step"] == 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO moment_public_keys(author_user_id, is_own, key_gen,"
            " key_step, key_sealed) VALUES('bob', 1, 1, -1, 'k')"
        )
