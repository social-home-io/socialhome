"""Migration 0123 — a reply stored public under a moment that isn't public
loses ``is_public``; nothing else changes."""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.repositories.moment_repo import _tombstone_rows
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


def _moment(c, mid, parent, public, via):
    c.execute(
        "INSERT INTO moments(id, author_user_id, content, parent_moment_id,"
        " origin_instance_id, expires_at, is_public, received_via)"
        " VALUES(?, 'u-a', 'x', ?, 'inst-a', datetime('now', '+1 day'), ?, ?)",
        (mid, parent, public, via),
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
    for row in _ROWS:
        _moment(c, *row)
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


# ─── The guard: a reply is public only under a public parent ─────────────

_REFUSED = "reply can be public only"


def test_the_guard_refuses_a_public_reply_under_a_household_moment(conn):
    with pytest.raises(sqlite3.IntegrityError, match=_REFUSED):
        _moment(conn, "r-new", "m-house", 1, "self")
    # …and under a parent this household doesn't hold.
    with pytest.raises(sqlite3.IntegrityError, match=_REFUSED):
        _moment(conn, "r-orphan", "m-gone", 1, "self")
    _moment(conn, "r-new", "m-house", 0, "self")


def test_the_guard_refuses_an_update_that_makes_such_a_reply_public(conn):
    with pytest.raises(sqlite3.IntegrityError, match=_REFUSED):
        conn.execute("UPDATE moments SET is_public=1 WHERE id='r-private'")
    # Moving a public reply under a household moment is refused too.
    with pytest.raises(sqlite3.IntegrityError, match=_REFUSED):
        conn.execute(
            "UPDATE moments SET parent_moment_id='m-house' WHERE id='r-public-ok'"
        )


def test_the_guard_lets_a_public_reply_under_a_public_parent_through(conn):
    _moment(conn, "r-pub-2", "m-pub", 1, "self")
    conn.execute("UPDATE moments SET content='edited' WHERE id='r-public-ok'")
    conn.execute("UPDATE moments SET is_public=1 WHERE id='r-pub-2'")


def test_the_guard_leaves_inbound_rows_alone(conn):
    # A public reply that arrived from a GFS or a household peer is stored
    # as received, whatever this household holds of its parent.
    _moment(conn, "r-gfs-2", "m-house", 1, "gfs")
    _moment(conn, "r-hh", "m-gone", 1, "household")
    conn.execute("UPDATE moments SET is_public=1 WHERE id='r-gfs'")


def test_deleting_a_public_parent_keeps_its_public_reply_writable(conn):
    # The tombstone detaches the reply (parent → NULL) before it clears
    # the parent's ``is_public``; neither step may trip the guard.
    conn.execute("BEGIN")
    assert _tombstone_rows(conn, "id=?", ("m-pub",)) == 1
    conn.execute("COMMIT")
    row = conn.execute(
        "SELECT parent_moment_id, is_public FROM moments WHERE id='r-public-ok'"
    ).fetchone()
    assert (row["parent_moment_id"], row["is_public"]) == (None, 1)
    # A tombstone of a public reply clears it without tripping either.
    conn.execute("BEGIN")
    assert _tombstone_rows(conn, "id=?", ("r-public-ok",)) == 1
    conn.execute("COMMIT")
