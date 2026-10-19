"""Migration 0108 — the paired-household directory keeps "unsure".

One column with a constant default: ``area_edge`` (0 / 1), set only on a
local community's row whose ``in_area`` is NULL.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 108

_INSERT = (
    "INSERT INTO peer_space_directory(instance_id, space_id, name, radius_km,"
    " in_area, area_edge) VALUES('peer', ?, 'Quartier', ?, ?, ?)"
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
    c.execute(
        "INSERT INTO peer_space_directory(instance_id, space_id, name, radius_km,"
        " in_area) VALUES('peer', 'old', 'Before', 2.0, 1)"
    )
    apply_through(c, _VERSION)
    yield c
    c.close()


def test_rows_cached_before_are_not_unsure(conn):
    row = conn.execute(
        "SELECT in_area, area_edge FROM peer_space_directory WHERE space_id='old'"
    ).fetchone()
    assert tuple(row) == (1, 0)


def test_unsure_is_stored_beside_an_unknown_verdict(conn):
    conn.execute(_INSERT, ("edge", 5.0, None, 1))
    row = conn.execute(
        "SELECT in_area, area_edge FROM peer_space_directory WHERE space_id='edge'"
    ).fetchone()
    assert tuple(row) == (None, 1)


@pytest.mark.parametrize(
    "radius,in_area,edge",
    [
        (5.0, 1, 1),  # beside a yes
        (5.0, 0, 1),  # beside a no
        (None, None, 1),  # on a row that is no local community
        (5.0, None, 2),  # not a flag
    ],
)
def test_an_ambiguous_row_is_refused(conn, radius, in_area, edge):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_INSERT, ("bad", radius, in_area, edge))
