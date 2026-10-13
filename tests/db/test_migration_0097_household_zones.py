"""Migration 0097 — ``household_zones``: the household's own zone list.

One new table; nothing existing changes. The DB enforces the radius
range, the 0/1 ``is_home`` flag, at most one Home zone, unique
(case-insensitive) names and unique ``external_id`` values (NULLs —
zones created in Social Home — allowed many times).
"""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

_VERSION = 97


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
    _apply_through(c, _VERSION)
    yield c
    c.close()


def _insert(
    c: sqlite3.Connection,
    zid: str,
    name: str,
    *,
    radius: int = 100,
    is_home: int = 0,
    external_id: str | None = None,
) -> None:
    c.execute(
        "INSERT INTO household_zones(id, name, latitude, longitude, radius_m,"
        " color, icon, is_home, external_id, created_at, updated_at)"
        " VALUES(?,?,?,?,?,NULL,NULL,?,?,'t','t')",
        (zid, name, 52.52, 13.405, radius, is_home, external_id),
    )


def test_table_exists_after_migration(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(household_zones)")}
    assert cols == {
        "id",
        "name",
        "latitude",
        "longitude",
        "radius_m",
        "color",
        "icon",
        "is_home",
        "external_id",
        "created_at",
        "updated_at",
    }


def test_migration_is_registered_in_order():
    versions = [m.version for m in discover_migrations()]
    assert _VERSION in versions
    assert versions.index(_VERSION) == versions.index(_VERSION - 1) + 1


@pytest.mark.parametrize("radius", [24, 50_001])
def test_radius_out_of_range_refused(conn, radius):
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, "z1", "School", radius=radius)


@pytest.mark.parametrize("radius", [25, 50_000])
def test_radius_bounds_accepted(conn, radius):
    _insert(conn, "z1", "School", radius=radius)


def test_is_home_must_be_zero_or_one(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, "z1", "Home", is_home=2)


def test_only_one_home_zone(conn):
    _insert(conn, "z1", "Home", is_home=1)
    _insert(conn, "z2", "School")
    _insert(conn, "z3", "Work")
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, "z4", "Other home", is_home=1)


def test_name_unique_case_insensitive(conn):
    _insert(conn, "z1", "School")
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, "z2", "school")


def test_external_id_unique_but_nulls_repeat(conn):
    _insert(conn, "z1", "A")
    _insert(conn, "z2", "B")
    _insert(conn, "z3", "C", external_id="zone.home")
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, "z4", "D", external_id="zone.home")
