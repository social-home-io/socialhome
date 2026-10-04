"""Migration 0076 — ``spaces.gfs_publish_mode`` + ``space_keys.writer_key``
(v_50 strict member publish).

Additive: an existing space reads ``trusted`` and an existing key row holds
no writer key.
"""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

_VERSION = 76


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
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp1','S','host','anna','ab')"
    )
    c.execute(
        "INSERT INTO space_keys(space_id, epoch, content_key_hex, writer_cert)"
        " VALUES('sp1', 3, 'wrapped', '{}')"
    )
    yield c
    c.close()


def test_existing_rows_read_trusted_and_no_writer_key(conn):
    _apply_through(conn, _VERSION)
    assert (
        conn.execute("SELECT gfs_publish_mode FROM spaces").fetchone()[0] == "trusted"
    )
    row = conn.execute(
        "SELECT content_key_hex, writer_cert, writer_key FROM space_keys"
    ).fetchone()
    assert (row["content_key_hex"], row["writer_cert"]) == ("wrapped", "{}")
    assert row["writer_key"] is None


def test_mode_check_constraint(conn):
    _apply_through(conn, _VERSION)
    conn.execute("UPDATE spaces SET gfs_publish_mode='strict'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE spaces SET gfs_publish_mode='open'")


def test_writer_key_cascades_with_the_space(conn):
    _apply_through(conn, _VERSION)
    conn.execute("UPDATE space_keys SET writer_key='w'")
    conn.execute("DELETE FROM spaces WHERE id='sp1'")
    assert conn.execute("SELECT COUNT(*) FROM space_keys").fetchone()[0] == 0
