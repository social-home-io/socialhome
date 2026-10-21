"""Migration 0116 — ``highlight_public_tokens``: the share tokens this
household minted for its public highlight links, so it streams only to a
link it still knows (not to any token a GFS forwards)."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 116


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
        "INSERT INTO highlights(id, author_user_id, highlight_date, expires_at)"
        " VALUES('h1', 'u1', '2026-10-20', '2026-10-27T00:00:00+00:00')"
    )
    c.execute(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name,"
        " public_key, inbox_url, status, paired_at)"
        " VALUES('g1', 'g1', 'G', 'ff', 'https://g', 'active', datetime('now'))"
    )
    apply_through(c, _VERSION)
    yield c
    c.close()


_INSERT = (
    "INSERT INTO highlight_public_tokens(token_hash, highlight_id, gfs_id)"
    " VALUES(?, ?, ?)"
)


def test_a_token_cascades_with_its_highlight(conn):
    conn.execute(_INSERT, ("t" * 64, "h1", "g1"))
    conn.execute("DELETE FROM highlights WHERE id='h1'")
    assert (
        conn.execute("SELECT COUNT(*) FROM highlight_public_tokens").fetchone()[0] == 0
    )


def test_a_token_cascades_with_its_gfs_connection(conn):
    conn.execute(_INSERT, ("t" * 64, "h1", "g1"))
    conn.execute("DELETE FROM gfs_connections WHERE id='g1'")
    assert (
        conn.execute("SELECT COUNT(*) FROM highlight_public_tokens").fetchone()[0] == 0
    )


@pytest.mark.parametrize(
    ("highlight_id", "gfs_id"), [("no-such", "g1"), ("h1", "no-such")]
)
def test_a_token_needs_its_highlight_and_gfs(conn, highlight_id, gfs_id):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_INSERT, ("t" * 64, highlight_id, gfs_id))


def test_a_token_hash_is_a_sha256_hex_digest(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_INSERT, ("short", "h1", "g1"))
