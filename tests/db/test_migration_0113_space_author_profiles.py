"""Migration 0113 — ``space_author_profiles``: a GFS follower's cache of a
space's author profiles, cascading with the space."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 113


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
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp', 'S', 'h', 'o', ?)",
        ("aa" * 32,),
    )
    apply_through(c, _VERSION)
    yield c
    c.close()


_INSERT = (
    "INSERT INTO space_author_profiles(space_id, author_user_id, author_pk,"
    " display_name, profile_rev, ts) VALUES(?, 'u', 'pk', 'Noor', 'r', 't')"
)


def test_a_profile_is_stored_and_cascades_with_its_space(conn):
    conn.execute(_INSERT, ("sp",))
    row = conn.execute("SELECT bio, picture_webp FROM space_author_profiles").fetchone()
    assert tuple(row) == (None, None)
    conn.execute("DELETE FROM spaces WHERE id='sp'")
    assert conn.execute("SELECT COUNT(*) FROM space_author_profiles").fetchone()[0] == 0


def test_a_profile_needs_a_space(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_INSERT, ("no-such-space",))
