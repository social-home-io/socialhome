"""GFS migration 0025 — the space media cache: one row per stored blob in
exactly one scope (its address is bound to it), per-scope bytes and running
totals kept by triggers (the caps are primary-key / index reads), an hourly
upload count per scope, an hourly byte budget per address, and the orphan
sweep's lease."""

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
    c.execute("PRAGMA foreign_keys=ON")
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    apply_through(c, 25, directory=_GFS_MIGRATIONS)
    yield c
    c.close()


_BLOB = (
    "INSERT INTO gfs_space_media(blob_id, scope_kind, scope_id, size, created_at,"
    " expires_at, last_fetched_at, household) VALUES(?, ?, ?, ?, 10, 20, 10, 'h1')"
)


def _plan(conn, sql: str) -> str:
    return " ".join(str(r[-1]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql))


def test_one_row_per_blob_in_one_scope_with_a_positive_size(conn):
    conn.execute(_BLOB, ("a" * 64, "space", "s1", 65_564))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_BLOB, ("a" * 64, "channel", "c1", 65_564))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_BLOB, ("b" * 64, "space", "s1", 0))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_BLOB, ("c" * 64, "room", "s1", 100))


def test_no_claims_owners_or_tombstones_any_more(conn):
    """Security review v2 (N1): the address binds the scope, so the
    cross-scope claim, owner and tombstone shapes are gone."""
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert (
        not {
            "gfs_space_media_claims",
            "gfs_space_media_tombstones",
            "gfs_space_media_owner_bytes",
        }
        & tables
    )


def test_totals_and_scope_bytes_follow_inserts_and_deletes(conn):
    def totals():
        return conn.execute(
            "SELECT blobs, bytes FROM gfs_space_media_totals WHERE id=1"
        ).fetchone()

    def scopes():
        return set(
            conn.execute(
                "SELECT scope_kind, scope_id, bytes FROM gfs_space_media_scope_bytes"
            ).fetchall()
        )

    assert totals() == (0, 0)
    conn.execute(_BLOB, ("a" * 64, "space", "s1", 100))
    conn.execute(_BLOB, ("b" * 64, "space", "s1", 300))
    conn.execute(_BLOB, ("c" * 64, "channel", "c1", 50))
    assert totals() == (3, 450)
    assert scopes() == {("space", "s1", 400), ("channel", "c1", 50)}
    assert conn.execute(
        "SELECT household, bytes FROM gfs_space_media_household_bytes"
    ).fetchall() == [("h1", 450)]
    conn.execute("DELETE FROM gfs_space_media WHERE blob_id=?", ("a" * 64,))
    conn.execute("DELETE FROM gfs_space_media WHERE blob_id=?", ("c" * 64,))
    assert totals() == (1, 300)
    assert conn.execute(
        "SELECT household, bytes FROM gfs_space_media_household_bytes"
    ).fetchall() == [("h1", 300)]
    # A scope at 0 bytes leaves no row.
    assert scopes() == {("space", "s1", 300)}


def test_upload_counts_are_one_row_per_scope_and_hour(conn):
    ins = (
        "INSERT INTO gfs_space_media_uploads(scope_kind, scope_id, hour_bucket,"
        " count) VALUES('space', 's1', 7, 1)"
    )
    conn.execute(ins)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins)


def test_ip_bytes_are_one_row_per_address_and_hour(conn):
    ins = (
        "INSERT INTO gfs_space_media_ip_bytes(ip_key, hour_bucket, bytes)"
        " VALUES('k', 7, 10)"
    )
    conn.execute(ins)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins)


def test_the_hot_reads_use_their_indexes(conn):
    assert "idx_gfs_space_media_scope" in _plan(
        conn,
        "SELECT blob_id FROM gfs_space_media WHERE scope_kind='space'"
        " AND scope_id='s1' ORDER BY last_fetched_at, created_at LIMIT 10",
    )
    assert "idx_gfs_space_media_scope_bytes" in _plan(
        conn,
        "SELECT scope_kind, scope_id, bytes FROM gfs_space_media_scope_bytes"
        " ORDER BY bytes DESC LIMIT 2",
    )
    assert "idx_gfs_space_media_household" in _plan(
        conn,
        "SELECT blob_id FROM gfs_space_media WHERE household='h1'"
        " ORDER BY last_fetched_at, created_at LIMIT 10",
    )
    assert "idx_gfs_space_media_household_bytes" in _plan(
        conn,
        "SELECT household, bytes FROM gfs_space_media_household_bytes"
        " ORDER BY bytes DESC LIMIT 2",
    )
    assert "idx_gfs_space_media_expires" in _plan(
        conn, "SELECT blob_id FROM gfs_space_media WHERE expires_at <= 5"
    )
    assert "idx_gfs_space_media_uploads_bucket" in _plan(
        conn, "DELETE FROM gfs_space_media_uploads WHERE hour_bucket < 5"
    )
    assert "idx_gfs_space_media_ip_bytes_bucket" in _plan(
        conn, "DELETE FROM gfs_space_media_ip_bytes WHERE hour_bucket < 5"
    )


def test_one_sweep_lease_row(conn):
    assert conn.execute(
        "SELECT holder, lease_until, cursor FROM gfs_space_media_sweep"
    ).fetchall() == [("", 0, "")]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO gfs_space_media_sweep(id) VALUES(2)")
