"""Migration 0122 — ``space_member_profile_pictures.sync_seq``."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 122


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
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp','sp','h','anna','00')"
    )
    c.execute(
        "INSERT INTO space_member_profile_pictures(space_id, user_id, bytes_webp,"
        " hash, width, height) VALUES('sp','u-old',x'00','h0',1,1)"
    )
    apply_through(c, _VERSION)
    yield c
    c.close()


def _seq(conn, user: str):
    return conn.execute(
        "SELECT sync_seq FROM space_member_profile_pictures WHERE user_id=?", (user,)
    ).fetchone()["sync_seq"]


def _upsert(conn, user: str, hash_: str, data: bytes = b"\x01") -> None:
    conn.execute(
        "INSERT INTO space_member_profile_pictures(space_id, user_id, bytes_webp,"
        " hash, width, height, updated_at) VALUES('sp',?,?,?,1,1,datetime('now'))"
        " ON CONFLICT(space_id, user_id) DO UPDATE SET bytes_webp=excluded.bytes_webp,"
        " hash=excluded.hash, width=excluded.width, height=excluded.height,"
        " updated_at=excluded.updated_at",
        (user, data, hash_),
    )


def test_an_existing_row_is_unstamped(conn):
    assert _seq(conn, "u-old") is None


def test_a_new_picture_is_stamped(conn):
    _upsert(conn, "u-new", "h1")
    assert _seq(conn, "u-new") is not None


def test_re_applying_the_same_picture_stamps_nothing(conn):
    """A receiver re-applying a streamed picture it holds rewrites only
    ``updated_at`` — it must not look changed, or two households would
    bounce it back and forth."""
    _upsert(conn, "u-a", "h1")
    first = _seq(conn, "u-a")
    _upsert(conn, "u-a", "h1")
    assert _seq(conn, "u-a") == first


def test_a_changed_picture_is_stamped_again(conn):
    _upsert(conn, "u-a", "h1")
    first = _seq(conn, "u-a")
    _upsert(conn, "u-a", "h2", b"\x02")
    assert _seq(conn, "u-a") > first
