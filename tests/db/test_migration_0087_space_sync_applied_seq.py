"""Migration 0087 — ``space_instances.applied_seq``: the requester's echo of
the provider snapshot of the last stream it applied cleanly (§25.6)."""

from __future__ import annotations

import sqlite3

from socialhome.db.migrations import discover_migrations

_VERSION = 87


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


def _fresh(tmp_path) -> sqlite3.Connection:
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    return c


def test_existing_seats_get_no_applied_seq(tmp_path):
    c = _fresh(tmp_path)
    _apply_through(c, _VERSION - 1)
    c.execute("INSERT INTO space_instances(space_id, instance_id) VALUES('sp','h')")
    c.execute("UPDATE space_instances SET synced_seq=7 WHERE instance_id='h'")
    _apply_through(c, _VERSION)
    row = c.execute("SELECT synced_seq, applied_seq FROM space_instances").fetchone()
    assert tuple(row) == (7, None)


def test_the_seat_upsert_keeps_the_applied_seq(tmp_path):
    c = _fresh(tmp_path)
    _apply_through(c, _VERSION)
    c.execute("INSERT INTO space_instances(space_id, instance_id) VALUES('sp','h')")
    c.execute("UPDATE space_instances SET applied_seq=42 WHERE instance_id='h'")
    c.execute(
        "INSERT INTO space_instances(space_id, instance_id) VALUES('sp','h')"
        " ON CONFLICT(space_id, instance_id) DO UPDATE SET"
        " last_seen_at=datetime('now')"
    )
    assert c.execute("SELECT applied_seq FROM space_instances").fetchone()[0] == 42
