"""Migration 0106 — a development database that ran the first revision of
0105 (which stored a paired household's local-community centre) ends with
the released shape; any other database is untouched."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 106

#: The never-released first revision of 0105, verbatim.
_OLD_0105 = """
ALTER TABLE peer_space_directory ADD COLUMN lat REAL;
ALTER TABLE peer_space_directory ADD COLUMN lon REAL;
ALTER TABLE peer_space_directory ADD COLUMN radius_km REAL
    CHECK (
        (lat IS NULL AND lon IS NULL AND radius_km IS NULL)
        OR (
            lat IS NOT NULL AND lon IS NOT NULL AND radius_km IS NOT NULL
            AND lat BETWEEN -90 AND 90
            AND lon BETWEEN -180 AND 180
            AND radius_km BETWEEN 0.5 AND 50
        )
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
    """A database that ran the first revision of 0105."""
    c = _fresh(tmp_path, "dev.db")
    apply_through(c, 104)
    c.executescript(_OLD_0105)
    c.execute(
        "INSERT INTO schema_version(version, description)"
        " VALUES(105, 'peer space directory local area')"
    )
    c.execute(
        "INSERT INTO peer_space_directory(instance_id, space_id, name, lat, lon,"
        " radius_km) VALUES('peer', 'q', 'Quartier', 47.3769, 8.5417, 5.0)"
    )
    c.execute(
        "INSERT INTO peer_space_directory(instance_id, space_id, name)"
        " VALUES('peer', 'plain', 'Garden')"
    )
    apply_through(c, _VERSION)
    yield c
    c.close()


def test_a_dev_database_ends_with_the_released_shape(dev, released):
    assert _table_sql(dev) == _table_sql(released)


def test_the_stored_centre_is_gone_and_the_rows_stay(dev):
    rows = {
        r["space_id"]: dict(r)
        for r in dev.execute("SELECT * FROM peer_space_directory")
    }
    assert set(rows) == {"q", "plain"}
    assert not {"lat", "lon"} & set(rows["q"])
    # The area went with the columns; the host's next snapshot refills it.
    assert rows["q"]["radius_km"] is None and rows["q"]["in_area"] is None


def test_the_current_writer_works_on_a_repaired_dev_database(dev):
    """The first revision's CHECK refused a radius without a centre — the
    row the current code writes."""
    dev.execute(
        "INSERT INTO peer_space_directory(instance_id, space_id, name, radius_km,"
        " in_area) VALUES('peer', 'n', 'New', 2.0, 1)"
    )


def test_a_released_database_is_untouched(tmp_path):
    c = _fresh(tmp_path, "r.db")
    apply_through(c, _VERSION - 1)
    before = _table_sql(c)
    apply_through(c, _VERSION)
    assert _table_sql(c) == before
    c.close()
