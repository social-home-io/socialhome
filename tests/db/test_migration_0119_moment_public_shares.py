"""Migration 0119 — per-moment share links: one row per public
moment, gone with the moment."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 119


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
    apply_through(c, _VERSION)
    c.execute(
        "INSERT INTO users(user_id, username, display_name, state)"
        " VALUES('u1', 'alice', 'Alice', 'active')"
    )
    c.execute(
        "INSERT INTO moments(id, author_user_id, content, origin_instance_id,"
        " created_at, expires_at) VALUES('m1', 'u1', 'x', 'i', 'a', 'b')"
    )
    yield c
    c.close()


def test_one_row_per_moment_cascading_with_it(conn):
    ins = (
        "INSERT INTO moment_public_shares(moment_id, author_user_id, item_key_sealed)"
        " VALUES(?, 'u1', 'k')"
    )
    conn.execute(ins, ("m1",))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("m1",))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("m-unknown",))
    conn.execute("DELETE FROM moments WHERE id='m1'")
    assert conn.execute("SELECT COUNT(*) FROM moment_public_shares").fetchone()[0] == 0
