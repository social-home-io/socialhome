"""Migration 0095 — ``gfs_channel_withdrawals``.

Additive: one new, empty table; a pending withdrawal per (space,
connection, channel), cascading with its connection.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 95
_CHANNEL = "ab" * 16


def _apply_through(conn: sqlite3.Connection, last: int) -> None:
    apply_through(conn, last)


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
    _apply_through(c, _VERSION - 1)
    c.execute(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name,"
        " public_key, inbox_url, status, paired_at)"
        " VALUES('c1','gfs-1','G','aa','https://gfs.example','active','t')"
    )
    _apply_through(c, _VERSION)
    yield c
    c.close()


def test_a_pending_withdrawal_round_trips_and_is_unique(conn):
    conn.execute(
        "INSERT INTO gfs_channel_withdrawals(space_id, gfs_connection_id,"
        " channel_id) VALUES('sp','c1',?)",
        (_CHANNEL,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO gfs_channel_withdrawals(space_id, gfs_connection_id,"
            " channel_id) VALUES('sp','c1',?)",
            (_CHANNEL,),
        )
    row = conn.execute("SELECT * FROM gfs_channel_withdrawals").fetchone()
    assert row["created_at"]


@pytest.mark.parametrize("bad", ["short", "ZZ" * 16, "ab" * 17])
def test_the_channel_id_is_checked(conn, bad):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO gfs_channel_withdrawals(space_id, gfs_connection_id,"
            " channel_id) VALUES('sp','c1',?)",
            (bad,),
        )


def test_it_cascades_with_its_connection_and_needs_one(conn):
    conn.execute(
        "INSERT INTO gfs_channel_withdrawals(space_id, gfs_connection_id,"
        " channel_id) VALUES('sp','c1',?)",
        (_CHANNEL,),
    )
    conn.execute("DELETE FROM gfs_connections WHERE id='c1'")
    assert (
        conn.execute("SELECT COUNT(*) FROM gfs_channel_withdrawals").fetchone()[0] == 0
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO gfs_channel_withdrawals(space_id, gfs_connection_id,"
            " channel_id) VALUES('sp','nope',?)",
            (_CHANNEL,),
        )
