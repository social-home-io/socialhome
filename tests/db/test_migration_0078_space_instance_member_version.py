"""Migration 0078 — ``space_instances.proto_version`` + ``identity_pk``.

Additive: an existing member-household row survives untouched with both
columns NULL (no version claim recorded — the pre-migration state).
"""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

_VERSION = 78


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
    c.execute("INSERT INTO space_instances(space_id, instance_id) VALUES('sp1', 'd')")
    yield c
    c.close()


def test_existing_row_survives_with_no_claim(conn):
    _apply_through(conn, _VERSION)
    row = conn.execute(
        "SELECT space_id, instance_id, proto_version, identity_pk FROM space_instances"
    ).fetchone()
    assert (row["space_id"], row["instance_id"]) == ("sp1", "d")
    assert row["proto_version"] is None
    assert row["identity_pk"] is None


def test_columns_are_writable(conn):
    _apply_through(conn, _VERSION)
    conn.execute(
        "UPDATE space_instances SET proto_version=51, identity_pk='ab'"
        " WHERE instance_id='d'"
    )
    row = conn.execute(
        "SELECT proto_version, identity_pk FROM space_instances"
    ).fetchone()
    assert (row[0], row[1]) == (51, "ab")
