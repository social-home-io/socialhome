"""Migration 0132 — the unused ``dm_contact_requests`` table (and its index)
is dropped. Direct chats between paired households need no request since
#1229; nothing reads or writes the table any more. Old rows go with it; a
``dm_contact_requested`` notification row stays untouched."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 132


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
        "INSERT INTO users(username, user_id, display_name) VALUES('anna', 'u-a', 'Anna')"
    )
    c.execute(
        "INSERT INTO dm_contact_requests(id, from_user_id, to_user_id)"
        " VALUES('r1', 'ru-ben', 'u-a')"
    )
    c.execute(
        "INSERT INTO notifications(id, user_id, type, title)"
        " VALUES('n1', 'u-a', 'dm_contact_requested', 'Ben wants to chat')"
    )
    apply_through(c, _VERSION)
    yield c
    c.close()


def _names(conn, kind: str) -> set[str]:
    return {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))
    }


def test_the_table_is_gone_even_with_rows_in_it(conn):
    assert "dm_contact_requests" not in _names(conn, "table")


def test_its_index_is_gone(conn):
    assert "idx_dm_contact_requests_to" not in _names(conn, "index")


def test_an_old_contact_request_notification_stays(conn):
    row = conn.execute("SELECT type, title FROM notifications WHERE id='n1'").fetchone()
    assert tuple(row) == ("dm_contact_requested", "Ben wants to chat")


def test_the_user_is_untouched(conn):
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
