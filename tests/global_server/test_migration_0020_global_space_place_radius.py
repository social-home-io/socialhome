"""GFS migration 0020 — a global space's optional radius hint: one
NULL-defaulted column, within 0.5–50 km and only beside a whole pin."""

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
    c.execute(
        "INSERT INTO global_spaces(space_id, owning_instance, name, place_lat,"
        " place_lon) VALUES('old', 'o', 'n', 47.0, 8.0)"
    )
    apply_through(c, 20, directory=_GFS_MIGRATIONS)
    yield c
    c.close()


_INSERT = (
    "INSERT INTO global_spaces(space_id, owning_instance, name, place_lat,"
    " place_lon, place_radius_km) VALUES(?, 'o', 'n', ?, ?, ?)"
)


def test_existing_pins_have_no_hint(conn):
    row = conn.execute(
        "SELECT place_radius_km FROM global_spaces WHERE space_id='old'"
    ).fetchone()
    assert row[0] is None


def test_a_hint_beside_a_pin_is_stored(conn):
    conn.execute(_INSERT, ("a", 47.0, 8.0, 10.0))
    conn.execute(_INSERT, ("b", 47.0, 8.0, None))


@pytest.mark.parametrize(
    "lat,lon,radius", [(None, None, 10.0), (47.0, 8.0, 0.1), (47.0, 8.0, 51.0)]
)
def test_a_stray_or_out_of_bounds_hint_is_refused(conn, lat, lon, radius):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_INSERT, ("bad", lat, lon, radius))
