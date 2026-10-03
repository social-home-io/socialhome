"""Migration 0074 — ``space_keys.writer_cert`` (v_49 space writer certs).

Additive: an existing key row survives untouched and holds no cert (NULL).
"""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

_VERSION = 74


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
        "INSERT INTO space_keys(space_id, epoch, content_key_hex)"
        " VALUES('sp1', 3, 'wrapped')"
    )
    yield c
    c.close()


def test_existing_key_row_survives_with_no_cert(conn):
    _apply_through(conn, _VERSION)
    row = conn.execute(
        "SELECT content_key_hex, writer_cert FROM space_keys WHERE space_id='sp1'"
    ).fetchone()
    assert row["content_key_hex"] == "wrapped"
    assert row["writer_cert"] is None


def test_cert_column_is_writable_and_cascades(conn):
    _apply_through(conn, _VERSION)
    conn.execute("UPDATE space_keys SET writer_cert='{}' WHERE space_id='sp1'")
    assert conn.execute("SELECT writer_cert FROM space_keys").fetchone()[0] == "{}"
    conn.execute("DELETE FROM spaces WHERE id='sp1'")
    assert conn.execute("SELECT COUNT(*) FROM space_keys").fetchone()[0] == 0
