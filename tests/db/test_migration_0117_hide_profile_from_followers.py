"""Migration 0117 — a person can hide their picture and bio from the
followers of global spaces they post in (shown by default)."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 117


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    apply_through(c, _VERSION - 1)
    c.execute("INSERT INTO preferences(id) VALUES('u-old')")
    apply_through(c, _VERSION)
    yield c
    c.close()


def _hidden(conn, row_id: str) -> int:
    return conn.execute(
        "SELECT hide_profile_from_followers FROM preferences WHERE id=?", (row_id,)
    ).fetchone()["hide_profile_from_followers"]


def test_an_existing_person_keeps_showing_their_profile(conn):
    assert _hidden(conn, "u-old") == 0


def test_a_new_row_defaults_to_shown(conn):
    conn.execute("INSERT INTO preferences(id) VALUES('u-new')")
    assert _hidden(conn, "u-new") == 0


def test_only_zero_or_one(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE preferences SET hide_profile_from_followers = 2 WHERE id='u-old'"
        )
