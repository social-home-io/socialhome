"""Migration 0124 — ``moment_saves`` (decision 6b): applies over existing
rows untouched, cascades with the person and the moment, and its triggers
drop saves on a tombstone and on the last unfollow of the author."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 124


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
        "INSERT INTO users(user_id, username, display_name) VALUES"
        " ('u-me', 'me', 'Me'), ('u-bob', 'bob', 'Bob')"
    )
    for mid, author in (("m-a", "u-a"), ("m-b", "u-b")):
        c.execute(
            "INSERT INTO moments(id, author_user_id, content, origin_instance_id,"
            " expires_at, received_via) VALUES(?, ?, 'x', 'inst',"
            " datetime('now', '+1 day'), 'household')",
            (mid, author),
        )
    before = [tuple(r) for r in c.execute("SELECT * FROM moments ORDER BY id")]
    apply_through(c, _VERSION)
    assert [tuple(r) for r in c.execute("SELECT * FROM moments ORDER BY id")] == before
    yield c
    c.close()


def _save(c, user: str, moment: str) -> None:
    c.execute(
        "INSERT INTO moment_saves(user_id, moment_id, saved_at)"
        " VALUES(?, ?, '2026-10-26T10:00:00+00:00')",
        (user, moment),
    )


def _saves(c) -> set[tuple[str, str]]:
    return {
        (r[0], r[1]) for r in c.execute("SELECT user_id, moment_id FROM moment_saves")
    }


def test_cascades_with_the_person_and_the_moment(conn):
    _save(conn, "u-me", "m-a")
    _save(conn, "u-bob", "m-b")
    conn.execute("DELETE FROM users WHERE user_id='u-bob'")
    conn.execute("DELETE FROM moments WHERE id='m-a'")
    assert _saves(conn) == set()


def test_a_save_needs_a_stored_moment_and_a_local_person(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _save(conn, "u-me", "m-never")
    with pytest.raises(sqlite3.IntegrityError):
        _save(conn, "u-stranger", "m-a")
    _save(conn, "u-me", "m-a")
    with pytest.raises(sqlite3.IntegrityError):
        _save(conn, "u-me", "m-a")


def test_a_tombstone_drops_every_save_of_it(conn):
    _save(conn, "u-me", "m-a")
    _save(conn, "u-bob", "m-a")
    _save(conn, "u-me", "m-b")
    conn.execute("UPDATE moments SET deleted_at=datetime('now') WHERE id='m-a'")
    assert _saves(conn) == {("u-me", "m-b")}


def test_the_last_unfollow_drops_saves_of_that_author(conn):
    conn.execute(
        "INSERT INTO user_follows(follower_user_id, followed_user_id)"
        " VALUES ('u-me', 'u-a'), ('u-bob', 'u-a')"
    )
    _save(conn, "u-me", "m-a")
    _save(conn, "u-bob", "m-a")
    _save(conn, "u-me", "m-b")
    conn.execute(
        "DELETE FROM user_follows WHERE follower_user_id='u-me'"
        " AND followed_user_id='u-a'"
    )
    # Only u-me's save of u-a's moment goes; Bob still follows and keeps his.
    assert _saves(conn) == {("u-bob", "m-a"), ("u-me", "m-b")}
