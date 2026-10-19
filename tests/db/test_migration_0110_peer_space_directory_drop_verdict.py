"""Migration 0110 — a development database that ran the first revisions
of 0105 / 0108 (a per-household ``in_area`` / ``area_edge`` verdict in the
paired-household directory) drops the two columns and ends with the released
shape; any other database is untouched."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 110

#: The never-released first revisions' columns, verbatim.
_OLD = """
ALTER TABLE peer_space_directory ADD COLUMN in_area INTEGER
    CHECK (in_area IS NULL OR (radius_km IS NOT NULL AND in_area IN (0, 1)));
ALTER TABLE peer_space_directory ADD COLUMN area_edge INTEGER NOT NULL DEFAULT 0
    CHECK (
        area_edge IN (0, 1)
        AND (area_edge = 0 OR (in_area IS NULL AND radius_km IS NOT NULL))
    );
"""


def _fresh(tmp_path, name: str) -> sqlite3.Connection:
    c = sqlite3.connect(tmp_path / name, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    return c


def _table_sql(c) -> str:
    return c.execute(
        "SELECT sql FROM sqlite_master WHERE name='peer_space_directory'"
    ).fetchone()[0]


@pytest.fixture
def released(tmp_path):
    c = _fresh(tmp_path, "released.db")
    apply_through(c, _VERSION)
    yield c
    c.close()


@pytest.fixture
def dev(tmp_path):
    c = _fresh(tmp_path, "dev.db")
    apply_through(c, _VERSION - 1)
    c.executescript(_OLD)
    c.execute(
        "INSERT INTO peer_space_directory(instance_id, space_id, name, radius_km,"
        " in_area, area_edge) VALUES('peer', 'q', 'Quartier', 5.0, NULL, 1)"
    )
    apply_through(c, _VERSION)
    yield c
    c.close()


def test_a_dev_database_ends_with_the_released_shape(dev, released):
    assert _table_sql(dev) == _table_sql(released)


def test_the_verdict_is_gone_and_the_rows_stay(dev):
    (row,) = [dict(r) for r in dev.execute("SELECT * FROM peer_space_directory")]
    assert row["space_id"] == "q" and row["radius_km"] == 5.0
    assert not {"in_area", "area_edge"} & set(row)


def test_a_released_database_is_untouched(tmp_path):
    c = _fresh(tmp_path, "r.db")
    apply_through(c, _VERSION - 1)
    before = _table_sql(c)
    apply_through(c, _VERSION)
    assert _table_sql(c) == before
    c.close()
