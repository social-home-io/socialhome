"""Migration 0082 — system chats on group-DM storage.

Additive: an existing conversation survives as a plain DM (``system_scope``
and ``space_id`` NULL), at most one household chat and one chat per space
can exist, a space's chat goes with the space, and the household toggle
defaults ON for an existing preferences row.
"""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

_VERSION = 82


def _apply_through(conn: sqlite3.Connection, last: int) -> None:
    done = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    for mig in discover_migrations():
        if mig.version in done:
            continue
        if mig.version > last:
            break
        mig.apply(conn)
        conn.execute(
            "INSERT INTO schema_version(version, description) VALUES (?,?)",
            (mig.version, mig.description),
        )


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
    c.execute("INSERT INTO conversations(id, type) VALUES('c-old', 'group_dm')")
    c.execute("INSERT INTO preferences(id) VALUES('household')")
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp1','S','host','anna','ab')"
    )
    yield c
    c.close()


def test_existing_conversation_stays_a_plain_dm(conn):
    _apply_through(conn, _VERSION)
    row = conn.execute(
        "SELECT id, type, system_scope, space_id FROM conversations"
    ).fetchone()
    assert tuple(row) == ("c-old", "group_dm", None, None)


def test_household_toggle_defaults_on_for_the_existing_row(conn):
    _apply_through(conn, _VERSION)
    row = conn.execute(
        "SELECT feat_household_chat FROM preferences WHERE id='household'"
    ).fetchone()
    assert row[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE preferences SET feat_household_chat=2")


def test_system_scope_is_a_closed_vocabulary(conn):
    _apply_through(conn, _VERSION)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO conversations(id, type, system_scope)"
            " VALUES('c-x', 'group_dm', 'everyone')"
        )


def test_only_one_household_chat(conn):
    _apply_through(conn, _VERSION)
    conn.execute(
        "INSERT INTO conversations(id, type, system_scope)"
        " VALUES('hh-1', 'group_dm', 'household')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO conversations(id, type, system_scope)"
            " VALUES('hh-2', 'group_dm', 'household')"
        )


def test_only_one_chat_per_space_and_it_goes_with_the_space(conn):
    _apply_through(conn, _VERSION)
    conn.execute(
        "INSERT INTO conversations(id, type, system_scope, space_id)"
        " VALUES('sc-1', 'group_dm', 'space', 'sp1')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO conversations(id, type, system_scope, space_id)"
            " VALUES('sc-2', 'group_dm', 'space', 'sp1')"
        )
    conn.execute("DELETE FROM spaces WHERE id='sp1'")
    assert (
        conn.execute("SELECT COUNT(*) FROM conversations WHERE id='sc-1'").fetchone()[0]
        == 0
    )
    # Person-made conversations are untouched by the cascade.
    assert (
        conn.execute("SELECT COUNT(*) FROM conversations WHERE id='c-old'").fetchone()[
            0
        ]
        == 1
    )


def test_space_chat_needs_a_known_space(conn):
    _apply_through(conn, _VERSION)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO conversations(id, type, system_scope, space_id)"
            " VALUES('sc-x', 'group_dm', 'space', 'nope')"
        )
