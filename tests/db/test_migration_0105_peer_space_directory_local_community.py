"""Migration 0105 — the paired-household directory marks local communities.

Two NULL-defaulted columns on ``peer_space_directory``: the radius (its
presence is the "local community" flag) and the host's in-area verdict
for this household. Never a centre.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 105

_INSERT = (
    "INSERT INTO peer_space_directory(instance_id, space_id, name, radius_km,"
    " in_area) VALUES('peer', ?, 'Quartier', ?, ?)"
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
    # A row cached before the migration keeps working, as no community.
    c.execute(
        "INSERT INTO peer_space_directory(instance_id, space_id, name)"
        " VALUES('peer', 'old', 'Before')"
    )
    apply_through(c, _VERSION)
    yield c
    c.close()


def _cols(c) -> set[str]:
    return {r[1] for r in c.execute("PRAGMA table_info(peer_space_directory)")}


def test_no_centre_columns(conn):
    """The centre never reaches a paired household, so there is nowhere to
    keep one."""
    cols = _cols(conn)
    assert {"radius_km", "in_area"} <= cols
    assert not {"lat", "lon", "latitude", "longitude"} & cols


def test_existing_rows_are_no_community(conn):
    row = conn.execute(
        "SELECT radius_km, in_area FROM peer_space_directory WHERE space_id='old'"
    ).fetchone()
    assert tuple(row) == (None, None)


@pytest.mark.parametrize("in_area", [None, 0, 1])
def test_a_community_with_any_verdict_is_stored(conn, in_area):
    conn.execute(_INSERT, ("s1", 5.0, in_area))
    row = conn.execute(
        "SELECT radius_km, in_area FROM peer_space_directory WHERE space_id='s1'"
    ).fetchone()
    assert tuple(row) == (5.0, in_area)


@pytest.mark.parametrize(
    "radius,in_area",
    [
        (None, 1),  # a verdict without a community
        (None, 0),
        (5.0, 2),  # not a boolean
        (0.1, None),  # out of range
        (51.0, 1),
    ],
)
def test_bad_rows_are_refused(conn, radius, in_area):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_INSERT, ("bad", radius, in_area))
