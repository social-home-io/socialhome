"""``tests.migration_chain.apply_through`` must be indistinguishable from
replaying the chain — otherwise a migration test runs on the wrong schema."""

from __future__ import annotations

import sqlite3

import pytest

from tests import migration_chain
from tests.db.connections import close_all, connect

_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_version ("
    " version INTEGER PRIMARY KEY, description TEXT,"
    " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
)


@pytest.fixture(autouse=True)
def _isolated_cache():
    migration_chain.clear()
    yield
    migration_chain.clear()
    close_all()


def _fresh(path) -> sqlite3.Connection:
    conn = connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(_DDL)
    return conn


def _state(conn: sqlite3.Connection) -> tuple:
    schema = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    versions = conn.execute(
        "SELECT version, description FROM schema_version ORDER BY version"
    ).fetchall()
    return schema, versions


@pytest.mark.parametrize("last", [40, 90, 10**6])
def test_a_restored_database_matches_a_replayed_one(tmp_path, last):
    replayed = _fresh(tmp_path / "replayed.db")
    migration_chain._replay(replayed, last, None)

    first = _fresh(tmp_path / "first.db")
    migration_chain.apply_through(first, last)  # builds the snapshot
    second = _fresh(tmp_path / "second.db")
    migration_chain.apply_through(second, last)  # restored from it

    assert _state(first) == _state(replayed)
    assert _state(second) == _state(replayed)
    assert len(migration_chain._snapshots) == 1
    # The restore is on disk, not just on this connection.
    second.close()
    assert _state(connect(tmp_path / "second.db")) == _state(replayed)


def test_a_seeded_database_takes_the_real_replay(tmp_path):
    conn = _fresh(tmp_path / "seeded.db")
    migration_chain.apply_through(conn, 40)
    conn.execute("CREATE TABLE extra (x INTEGER)")
    migration_chain.apply_through(conn, 60)  # partially migrated → replay
    assert [k[0] for k in migration_chain._snapshots] == [40]
    replayed = _fresh(tmp_path / "replayed.db")
    migration_chain._replay(replayed, 60, None)
    assert _state(conn)[1] == _state(replayed)[1]


def test_the_connection_settings_are_part_of_the_key(tmp_path):
    on = _fresh(tmp_path / "on.db")
    off = connect(tmp_path / "off.db", isolation_level=None)
    off.execute("PRAGMA foreign_keys=OFF")
    off.execute(_DDL)
    migration_chain.apply_through(on, 40)
    migration_chain.apply_through(off, 40)
    assert len(migration_chain._snapshots) == 2
    assert off.execute("PRAGMA foreign_keys").fetchone()[0] == 0
