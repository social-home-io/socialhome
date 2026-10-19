"""Migration 0103 — ``space_members.feed_read_at``, the per-seat feed marker.

Additive: existing seats keep every column and get NULL (never looked —
the Spaces list counts nothing new for them until the first Feed visit).
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 103


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
    apply_through(c, _VERSION - 1)
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, space_type, join_mode)"
        " VALUES('sp', 'Garden', 'inst', 'anna', 'ab', 'private', 'invite_only')"
    )
    c.execute(
        "INSERT INTO space_members(space_id, user_id, role, space_display_name)"
        " VALUES('sp', 'u1', 'admin', 'Anna')"
    )
    yield c
    c.close()


def test_existing_seats_get_a_null_marker_and_keep_the_rest(conn):
    apply_through(conn, _VERSION)
    row = conn.execute(
        "SELECT role, space_display_name, feed_read_at FROM space_members"
    ).fetchone()
    assert (row["role"], row["space_display_name"], row["feed_read_at"]) == (
        "admin",
        "Anna",
        None,
    )


def test_the_marker_goes_with_the_seat(conn):
    apply_through(conn, _VERSION)
    conn.execute("UPDATE space_members SET feed_read_at = datetime('now')")
    conn.execute("DELETE FROM spaces WHERE id = 'sp'")
    assert conn.execute("SELECT COUNT(*) FROM space_members").fetchone()[0] == 0
