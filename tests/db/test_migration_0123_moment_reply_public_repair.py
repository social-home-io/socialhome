"""Migration 0123 — a reply stored public under a moment that isn't public
loses ``is_public``; nothing else changes."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 123

_ROWS = (
    # id, parent, is_public, received_via
    ("m-house", None, 0, "self"),
    ("m-pub", None, 1, "self"),
    ("r-leaked", "m-house", 1, "self"),
    ("r-private", "m-house", 0, "self"),
    ("r-public-ok", "m-pub", 1, "self"),
    # A stranger's public reply a follower holds is not this household's
    # to publish: left as received.
    ("r-gfs", "m-house", 1, "gfs"),
)


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
    for mid, parent, public, via in _ROWS:
        c.execute(
            "INSERT INTO moments(id, author_user_id, content, parent_moment_id,"
            " origin_instance_id, expires_at, is_public, received_via)"
            " VALUES(?, 'u-a', 'x', ?, 'inst-a', datetime('now', '+1 day'), ?, ?)",
            (mid, parent, public, via),
        )
    apply_through(c, _VERSION)
    yield c
    c.close()


def test_only_a_leaked_reply_loses_is_public(conn):
    rows = {
        r["id"]: r["is_public"]
        for r in conn.execute("SELECT id, is_public FROM moments").fetchall()
    }
    assert rows == {
        "m-house": 0,
        "m-pub": 1,
        "r-leaked": 0,
        "r-private": 0,
        "r-public-ok": 1,
        "r-gfs": 1,
    }
