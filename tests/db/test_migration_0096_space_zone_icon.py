"""Migration 0096 — ``space_zones.icon`` (an optional single emoji).

Additive: one NULL-default column; existing zones keep every value and
start without an icon. The ``space_zones_sync_seq_update`` trigger is
recreated so an icon-only edit still bumps the §25.6 change stamp.
"""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

_VERSION = 96


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
        " identity_public_key) VALUES('sp','S','host','anna','ab')"
    )
    c.execute(
        "INSERT INTO space_zones(id, space_id, name, latitude, longitude,"
        " radius_m, color, created_by) VALUES('z1','sp','School',1.0,2.0,"
        " 100,'#3b82f6','u1')"
    )
    yield c
    c.close()


def _seq(c: sqlite3.Connection) -> int:
    return c.execute("SELECT sync_seq FROM space_zones WHERE id='z1'").fetchone()[0]


def test_existing_zone_keeps_its_row_and_has_no_icon(conn):
    _apply_through(conn, _VERSION)
    row = conn.execute("SELECT * FROM space_zones WHERE id='z1'").fetchone()
    assert row["name"] == "School"
    assert row["color"] == "#3b82f6"
    assert row["icon"] is None


def test_column_is_nullable_text(conn):
    _apply_through(conn, _VERSION)
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(space_zones)")}
    assert cols["icon"]["type"].upper() == "TEXT"
    assert cols["icon"]["notnull"] == 0
    assert cols["icon"]["dflt_value"] is None


def test_icon_only_edit_bumps_sync_seq(conn):
    _apply_through(conn, _VERSION)
    before = _seq(conn)
    conn.execute("UPDATE space_zones SET icon='🏫' WHERE id='z1'")
    after_set = _seq(conn)
    assert after_set > before
    conn.execute("UPDATE space_zones SET icon=NULL WHERE id='z1'")
    assert _seq(conn) > after_set


def test_idempotent_reapply_does_not_bump_sync_seq(conn):
    _apply_through(conn, _VERSION)
    conn.execute("UPDATE space_zones SET icon='🏫' WHERE id='z1'")
    before = _seq(conn)
    conn.execute("UPDATE space_zones SET icon='🏫' WHERE id='z1'")
    assert _seq(conn) == before
