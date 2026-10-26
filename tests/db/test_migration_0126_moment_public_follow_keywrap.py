"""Migration 0126 — a public-moment follow keeps the author household's
key-wrap key (v_68), NULL until a signed envelope names it; the CHECK keeps
it a lowercase hex key of a sane length."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 126


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
        "INSERT INTO users(user_id, username, display_name, state)"
        " VALUES('u1', 'alice', 'Alice', 'active')"
    )
    c.execute(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name,"
        " public_key, inbox_url, status, paired_at) VALUES('g1', 'gfs-1', 'G',"
        " 'aa', 'https://g.example', 'active', datetime('now'))"
    )
    c.execute(
        "INSERT INTO moment_public_follows(follower_user_id, followed_user_id,"
        " gfs_id, followed_instance_pk, followed_username, followed_display_name)"
        " VALUES('u1', 'u-noor', 'g1', 'bb', 'noor', 'Noor')"
    )
    apply_through(c, _VERSION)
    yield c
    c.close()


def test_an_existing_follow_survives_without_a_key(conn):
    row = conn.execute(
        "SELECT followed_display_name, followed_keywrap_pk FROM moment_public_follows"
    ).fetchone()
    assert tuple(row) == ("Noor", None)


def test_a_hex_key_is_stored(conn):
    conn.execute("UPDATE moment_public_follows SET followed_keywrap_pk=?", ("ab" * 32,))
    row = conn.execute(
        "SELECT followed_keywrap_pk FROM moment_public_follows"
    ).fetchone()
    assert row[0] == "ab" * 32


@pytest.mark.parametrize(
    "bad",
    ["ab" * 8, "AB" * 32, "zz" * 32, "a" * 65, "ab" * 2049, ""],
)
def test_anything_but_a_lowercase_hex_key_is_refused(conn, bad):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE moment_public_follows SET followed_keywrap_pk=?", (bad,))
