"""Migration 0072 — ``space_pages.seq`` + ``pending_base_seq`` (federation
v_48, host-sequenced pages). Additive: existing rows survive and read as
never sequenced (0) with no draft (NULL)."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 72


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


def test_existing_pages_survive_unsequenced_without_a_draft(conn):
    conn.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp1','S','host','anna','ab')"
    )
    conn.execute(
        "INSERT INTO space_pages(id, space_id, title, content, created_by)"
        " VALUES('pg1','sp1','T','body','u1')"
    )
    _apply_through(conn, _VERSION)
    row = conn.execute(
        "SELECT title, content, seq, pending_base_seq FROM space_pages WHERE id='pg1'"
    ).fetchone()
    assert tuple(row) == ("T", "body", 0, None)


def test_new_rows_default_to_seq_zero(conn):
    _apply_through(conn, _VERSION)
    conn.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp1','S','host','anna','ab')"
    )
    conn.execute(
        "INSERT INTO space_pages(id, space_id, title, content, created_by, seq)"
        " VALUES('pg1','sp1','T','b','u1', 3)"
    )
    row = conn.execute("SELECT seq, pending_base_seq FROM space_pages").fetchone()
    assert tuple(row) == (3, None)
