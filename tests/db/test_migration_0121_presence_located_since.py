"""Migration 0121 — ``presence.located_since``."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 121


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
    c.execute(
        "INSERT INTO users(username, user_id, display_name) VALUES('anna','u-a','A')"
    )
    c.execute(
        "INSERT INTO presence(username, entity_id, state, updated_at)"
        " VALUES('anna', 'anna', 'home', '2026-10-22 08:00:00')"
    )
    apply_through(c, _VERSION)
    yield c
    c.close()


def test_an_existing_row_has_no_located_since_yet(conn):
    row = conn.execute(
        "SELECT located_since, updated_at FROM presence WHERE username='anna'"
    ).fetchone()
    assert row["located_since"] is None
    assert row["updated_at"] == "2026-10-22 08:00:00"
