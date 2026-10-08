"""Migration 0084 — ``space_posts.moderated_by`` (who removed a post).

Additive: an existing row keeps everything and reads ``NULL``; a new row
may name the moderator.
"""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

_VERSION = 84


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
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp','S','host','anna','ab')"
    )
    c.execute(
        "INSERT INTO space_posts(id, space_id, author, type, deleted, moderated)"
        " VALUES('p-old','sp','u-a','text',1,1)"
    )
    yield c
    c.close()


def test_an_existing_row_keeps_its_state_and_names_nobody(conn):
    _apply_through(conn, _VERSION)
    row = conn.execute(
        "SELECT deleted, moderated, moderated_by FROM space_posts WHERE id='p-old'"
    ).fetchone()
    assert (row["deleted"], row["moderated"], row["moderated_by"]) == (1, 1, None)


def test_a_new_row_may_name_its_moderator(conn):
    _apply_through(conn, _VERSION)
    conn.execute(
        "INSERT INTO space_posts(id, space_id, author, type, moderated_by)"
        " VALUES('p-new','sp','u-a','text','u-mod')"
    )
    row = conn.execute(
        "SELECT moderated_by FROM space_posts WHERE id='p-new'"
    ).fetchone()
    assert row["moderated_by"] == "u-mod"
