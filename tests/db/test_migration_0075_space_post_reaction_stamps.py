"""Migration 0075 — ``space_posts.reaction_stamps_json`` (v_49 member relay).

Additive: an existing post survives untouched, its reactions intact, and
holds no stamps (NULL — the state before any relayed reaction).
"""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

_VERSION = 75


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
        " identity_public_key) VALUES('sp1','S','host','anna','ab')"
    )
    c.execute(
        "INSERT INTO space_posts(id, space_id, author, type, content, reactions)"
        " VALUES('p1', 'sp1', 'u1', 'text', 'hi', '{\"x\": [\"u2\"]}')"
    )
    yield c
    c.close()


def test_existing_post_survives_with_no_stamps(conn):
    _apply_through(conn, _VERSION)
    row = conn.execute(
        "SELECT content, reactions, reaction_stamps_json FROM space_posts WHERE id='p1'"
    ).fetchone()
    assert (row["content"], row["reactions"]) == ("hi", '{"x": ["u2"]}')
    assert row["reaction_stamps_json"] is None
