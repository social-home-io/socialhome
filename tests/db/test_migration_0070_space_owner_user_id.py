"""Migration 0070 — ``spaces.owner_user_id`` (federation v_47, forwarded
role changes): a stub records the owner's seat from the host's roster.

Additive: an existing space row survives untouched and reads NULL; new rows
default to NULL.
"""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

_VERSION = 70


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
    yield c
    c.close()


def test_existing_rows_survive_and_read_null(conn):
    conn.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp1','S','host','anna','ab')"
    )
    _apply_through(conn, _VERSION)
    row = conn.execute(
        "SELECT owner_username, identity_public_key, owner_user_id"
        " FROM spaces WHERE id='sp1'"
    ).fetchone()
    assert tuple(row) == ("anna", "ab", None)


def test_the_column_takes_a_user_id(conn):
    _apply_through(conn, _VERSION)
    conn.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, owner_user_id) VALUES('sp2','S','h','u','ab','u-1')"
    )
    row = conn.execute("SELECT owner_user_id FROM spaces WHERE id='sp2'").fetchone()
    assert row["owner_user_id"] == "u-1"
