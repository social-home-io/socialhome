"""Migration 0105 — the paired-household directory marks local communities.

One NULL-defaulted column on ``peer_space_directory``: the radius (its
presence is the "local community" flag). Never a centre, never a verdict.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 105

_INSERT = (
    "INSERT INTO peer_space_directory(instance_id, space_id, name, radius_km)"
    " VALUES('peer', ?, 'Quartier', ?)"
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


def test_the_radius_only(conn):
    """The centre never reaches a paired household, and whether a home is
    inside is the host's decision on a join — nowhere to keep either."""
    cols = _cols(conn)
    assert "radius_km" in cols
    assert not {"lat", "lon", "latitude", "longitude", "in_area", "area_edge"} & cols


def test_existing_rows_are_no_community(conn):
    row = conn.execute(
        "SELECT radius_km FROM peer_space_directory WHERE space_id='old'"
    ).fetchone()
    assert row[0] is None


def test_a_community_is_stored(conn):
    conn.execute(_INSERT, ("s1", 5.0))
    row = conn.execute(
        "SELECT radius_km FROM peer_space_directory WHERE space_id='s1'"
    ).fetchone()
    assert row[0] == 5.0


@pytest.mark.parametrize("radius", [0.1, 51.0])
def test_a_radius_out_of_range_is_refused(conn, radius):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_INSERT, ("bad", radius))
