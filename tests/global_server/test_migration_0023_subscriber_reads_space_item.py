"""GFS migration 0023 — one NULL-defaulted column per seat: whether the
seat's household reads ``space_item`` (its own word, from ``accepts``)."""

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
    apply_through(c, 22, directory=_GFS_MIGRATIONS)
    c.execute(
        "INSERT INTO client_instances(instance_id, display_name, public_key)"
        " VALUES('old.home', 'Old', 'aa')"
    )
    c.execute(
        "INSERT INTO global_spaces(space_id, owning_instance, name)"
        " VALUES('sp', 'o', 'n')"
    )
    c.execute(
        "INSERT INTO space_subscribers(space_id, instance_id) VALUES('sp', 'old.home')"
    )
    apply_through(c, 23, directory=_GFS_MIGRATIONS)
    yield c
    c.close()


def test_a_seat_taken_before_is_unknown(conn):
    """NULL — counted as "does not read it" (the relay keeps going)."""
    row = conn.execute(
        "SELECT reads_space_item FROM space_subscribers WHERE instance_id='old.home'"
    ).fetchone()
    assert row[0] is None


def test_a_seat_records_its_households_word(conn):
    conn.execute(
        "UPDATE space_subscribers SET reads_space_item=1 WHERE instance_id='old.home'"
    )
    row = conn.execute("SELECT reads_space_item FROM space_subscribers").fetchone()
    assert row[0] == 1
