"""GFS migration 0019 — a global space's optional place pin: two
NULL-defaulted columns, a whole in-range point or none."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.migration_chain import apply_through

_GFS_MIGRATIONS = (
    Path(__file__).resolve().parent.parent.parent
    / "socialhome/global_server/migrations"
)


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "gfs.db", isolation_level=None)
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    apply_through(c, 19, directory=_GFS_MIGRATIONS)
    yield c
    c.close()


_INSERT = (
    "INSERT INTO global_spaces(space_id, owning_instance, name, place_lat,"
    " place_lon) VALUES(?, 'o', 'n', ?, ?)"
)


def test_migration_0019_stores_a_whole_point_or_none(conn):
    conn.execute(_INSERT, ("a", 47.3769, 8.5417))
    conn.execute(_INSERT, ("b", None, None))
    for i, (lat, lon) in enumerate(
        [(47.0, None), (None, 8.0), (91.0, 8.0), (47.0, 181.0)]
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(_INSERT, (f"bad{i}", lat, lon))
