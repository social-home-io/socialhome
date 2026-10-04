"""Migration 0077 — ``spaces.gfs_channel_id`` / ``gfs_channel_pk`` and
``space_keys.gfs_channel`` (v_51 private-space channels).

Additive: an existing space has no channel and an existing key row holds no
grant; several spaces may name one channel id (no first-come claim).
"""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

_VERSION = 77


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
    for sid in ("sp1", "sp2"):
        c.execute(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key) VALUES(?,'S','host','anna','ab')",
            (sid,),
        )
    c.execute(
        "INSERT INTO space_keys(space_id, epoch, content_key_hex, writer_cert)"
        " VALUES('sp1', 3, 'wrapped', '{}')"
    )
    yield c
    c.close()


def test_existing_rows_hold_no_channel(conn):
    _apply_through(conn, _VERSION)
    row = conn.execute("SELECT gfs_channel_id, gfs_channel_pk FROM spaces").fetchone()
    assert tuple(row) == (None, None)
    assert conn.execute("SELECT gfs_channel FROM space_keys").fetchone()[0] is None


def test_several_spaces_may_name_one_channel(conn):
    """Deliberately not unique: another space's owner must not be able to
    claim this space's channel id first (inbound frames try each candidate
    and the content key decides)."""
    _apply_through(conn, _VERSION)
    conn.execute("UPDATE spaces SET gfs_channel_id='c' WHERE id='sp1'")
    conn.execute("UPDATE spaces SET gfs_channel_id='c' WHERE id='sp2'")
    rows = conn.execute("SELECT id FROM spaces WHERE gfs_channel_id='c'").fetchall()
    assert len(rows) == 2
