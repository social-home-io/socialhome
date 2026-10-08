"""Migration 0083 — ``spaces.feature_chat`` (the per-space chat toggle).

Additive: an existing space gets the chat ON (owner decision), a new
space defaults ON, and the column only holds 0 / 1.
"""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

_VERSION = 83


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
        " identity_public_key) VALUES('sp-old','S','host','anna','ab')"
    )
    yield c
    c.close()


def test_existing_space_gets_the_chat_on(conn):
    _apply_through(conn, _VERSION)
    row = conn.execute("SELECT feature_chat FROM spaces WHERE id='sp-old'").fetchone()
    assert row["feature_chat"] == 1


def test_new_space_defaults_on(conn):
    _apply_through(conn, _VERSION)
    conn.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-new','N','host','anna','ab')"
    )
    row = conn.execute("SELECT feature_chat FROM spaces WHERE id='sp-new'").fetchone()
    assert row["feature_chat"] == 1


def test_admin_can_turn_it_off(conn):
    _apply_through(conn, _VERSION)
    conn.execute("UPDATE spaces SET feature_chat=0 WHERE id='sp-old'")
    row = conn.execute("SELECT feature_chat FROM spaces WHERE id='sp-old'").fetchone()
    assert row["feature_chat"] == 0


def test_only_zero_or_one(conn):
    _apply_through(conn, _VERSION)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE spaces SET feature_chat=2 WHERE id='sp-old'")
