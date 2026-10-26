"""Migration 0126 — a public-moment follow keeps the author household's
key-wrap key (v_68) and the KEM suite it was named under, both NULL until a
signed envelope names them; the CHECKs keep the key a lowercase hex key of a
sane length and never let a key be stored without its suite (or a suite
without a key)."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 126

_SET = (
    "UPDATE moment_public_follows SET followed_keywrap_pk=?, followed_keywrap_suite=?"
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
        "SELECT followed_display_name, followed_keywrap_pk, followed_keywrap_suite"
        " FROM moment_public_follows"
    ).fetchone()
    assert tuple(row) == ("Noor", None, None)


def test_a_hex_key_is_stored_with_its_suite(conn):
    conn.execute(_SET, ("ab" * 32, "x25519"))
    row = conn.execute(
        "SELECT followed_keywrap_pk, followed_keywrap_suite FROM moment_public_follows"
    ).fetchone()
    assert tuple(row) == ("ab" * 32, "x25519")
    # A longer (PQ-hybrid) key under its own suite tag fits too.
    conn.execute(_SET, ("ab" * 1216, "x25519+mlkem768"))


@pytest.mark.parametrize(
    "bad",
    ["ab" * 8, "AB" * 32, "zz" * 32, "a" * 65, "ab" * 2049, ""],
)
def test_anything_but_a_lowercase_hex_key_is_refused(conn, bad):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_SET, (bad, "x25519"))


@pytest.mark.parametrize(
    ("pk", "suite"),
    [("ab" * 32, None), (None, "x25519"), ("ab" * 32, ""), ("ab" * 32, "s" * 33)],
)
def test_a_key_is_never_stored_without_its_suite(conn, pk, suite):
    """#1179: the key's suite is stored with it, so a key never has to be
    read under an assumed suite."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_SET, (pk, suite))
