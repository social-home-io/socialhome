"""Migration 0105 — the paired-household directory keeps a local area.

Three NULL-defaulted columns on ``peer_space_directory``; a row holds a
whole in-range area or none.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 105

_INSERT = (
    "INSERT INTO peer_space_directory(instance_id, space_id, name, lat, lon,"
    " radius_km) VALUES('peer', ?, 'Quartier', ?, ?, ?)"
)


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
    # A row cached before the migration keeps working, with no area.
    c.execute(
        "INSERT INTO peer_space_directory(instance_id, space_id, name)"
        " VALUES('peer', 'old', 'Before')"
    )
    apply_through(c, _VERSION)
    yield c
    c.close()


def test_existing_rows_have_no_area(conn):
    row = conn.execute(
        "SELECT lat, lon, radius_km FROM peer_space_directory WHERE space_id='old'"
    ).fetchone()
    assert tuple(row) == (None, None, None)


def test_a_whole_area_is_stored(conn):
    conn.execute(_INSERT, ("s1", 47.3769, 8.5417, 5.0))
    row = conn.execute(
        "SELECT lat, lon, radius_km FROM peer_space_directory WHERE space_id='s1'"
    ).fetchone()
    assert tuple(row) == (47.3769, 8.5417, 5.0)


@pytest.mark.parametrize(
    "lat,lon,radius",
    [
        (47.0, None, 5.0),  # half an area
        (None, None, 5.0),
        (47.0, 8.0, None),
        (91.0, 8.0, 5.0),  # out of range
        (47.0, 181.0, 5.0),
        (47.0, 8.0, 0.1),
        (47.0, 8.0, 51.0),
    ],
)
def test_half_or_out_of_range_areas_are_refused(conn, lat, lon, radius):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_INSERT, ("bad", lat, lon, radius))
