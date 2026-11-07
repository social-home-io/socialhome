"""GFS migration 0027 — space media wants offered to a listed space's
members (the member fallback of space media on demand): one row per open
session in its member phase, shared by the cluster so a pull on any node
sees it and exactly one member claims it. No column names a puller."""

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
    apply_through(c, 27, directory=_GFS_MIGRATIONS)
    yield c
    c.close()


_INS = (
    "INSERT INTO gfs_space_media_member_wants(ticket, scope_kind, scope_id,"
    " blob_id, mode, hints, expires_at, created_at) VALUES(?,?,?,?,?,?,?,?)"
)


def test_table_shape_names_no_puller(conn):
    cols = {
        r[1] for r in conn.execute("PRAGMA table_info(gfs_space_media_member_wants)")
    }
    assert cols == {
        "ticket",
        "scope_kind",
        "scope_id",
        "blob_id",
        "mode",
        "hints",
        "expires_at",
        "claimed_until",
        "attempts",
        "created_at",
        # The hash of a claim's random secret — names no one.
        "claim_hash",
    }
    # By design: nothing that could name the member that pulls or serves.
    assert not cols & {"instance_id", "public_key", "client", "ip", "puller"}


def test_one_row_per_ticket_and_only_listed_spaces(conn):
    conn.execute(_INS, ("t1", "space", "s1", "a" * 64, "stream", "[]", 10, 1))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_INS, ("t1", "space", "s1", "b" * 64, "stream", "[]", 10, 1))
    # A private channel has its own seat-holder path: never a member want.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_INS, ("t2", "channel", "c1", "a" * 64, "stream", "[]", 10, 1))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_INS, ("t3", "space", "s1", "a" * 64, "forever", "[]", 10, 1))
    row = conn.execute(
        "SELECT claimed_until, attempts FROM gfs_space_media_member_wants"
        " WHERE ticket='t1'"
    ).fetchone()
    assert row == (0, 0)


def test_indexes_for_the_claim_and_the_prune(conn):
    idx = {
        r[1] for r in conn.execute("PRAGMA index_list(gfs_space_media_member_wants)")
    }
    assert "idx_gfs_space_media_member_wants_scope" in idx
    assert "idx_gfs_space_media_member_wants_expires" in idx
