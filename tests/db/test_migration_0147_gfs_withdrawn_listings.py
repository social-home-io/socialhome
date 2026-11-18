"""Migration 0147 — ``gfs_withdrawn_listings`` and the retired channel's
wrapped signing seed.

Additive: one new, empty table (a withdrawn listing whose pin a GFS still
holds, cascading with its connection) and one NULL-default column.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 147
_KEY = "ab" * 32
_CHANNEL = "cd" * 16


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
    apply_through(c, _VERSION - 1)
    c.execute(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name,"
        " public_key, inbox_url, status, paired_at)"
        " VALUES('c1','gfs-1','G','aa','https://gfs.example','active','t')"
    )
    # A pending channel withdrawal from before the migration keeps its row.
    c.execute(
        "INSERT INTO gfs_channel_withdrawals(space_id, gfs_connection_id,"
        " channel_id) VALUES('sp','c1',?)",
        (_CHANNEL,),
    )
    apply_through(c, _VERSION)
    yield c
    c.close()


def test_a_withdrawn_listing_round_trips_and_is_unique(conn):
    conn.execute(
        "INSERT INTO gfs_withdrawn_listings(space_id, gfs_connection_id,"
        " pinned_key) VALUES('sp','c1',?)",
        (_KEY,),
    )
    conn.execute(
        "INSERT INTO gfs_withdrawn_listings(space_id, gfs_connection_id)"
        " VALUES('sp-2','c1')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO gfs_withdrawn_listings(space_id, gfs_connection_id)"
            " VALUES('sp','c1')"
        )
    rows = conn.execute(
        "SELECT * FROM gfs_withdrawn_listings ORDER BY space_id"
    ).fetchall()
    assert [(r["space_id"], r["pinned_key"]) for r in rows] == [
        ("sp", _KEY),
        ("sp-2", None),
    ]
    assert rows[0]["created_at"]


@pytest.mark.parametrize("bad", ["short", "ZZ" * 32, "ab" * 33, ""])
def test_the_pinned_key_is_checked(conn, bad):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO gfs_withdrawn_listings(space_id, gfs_connection_id,"
            " pinned_key) VALUES('sp','c1',?)",
            (bad,),
        )


def test_it_cascades_with_its_connection_and_needs_one(conn):
    conn.execute(
        "INSERT INTO gfs_withdrawn_listings(space_id, gfs_connection_id)"
        " VALUES('sp','c1')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO gfs_withdrawn_listings(space_id, gfs_connection_id)"
            " VALUES('sp','nope')"
        )
    conn.execute("DELETE FROM gfs_connections WHERE id='c1'")
    count = conn.execute("SELECT COUNT(*) FROM gfs_withdrawn_listings").fetchone()[0]
    assert count == 0


def test_a_channel_withdrawal_gains_a_null_seed_column(conn):
    row = conn.execute("SELECT * FROM gfs_channel_withdrawals").fetchone()
    assert row["channel_id"] == _CHANNEL
    assert row["channel_seed"] is None
    conn.execute("UPDATE gfs_channel_withdrawals SET channel_seed='wrapped'")
    stored = conn.execute(
        "SELECT channel_seed FROM gfs_channel_withdrawals"
    ).fetchone()[0]
    assert stored == "wrapped"
