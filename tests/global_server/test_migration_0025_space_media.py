"""GFS migration 0025 — the space media cache: one row per stored blob, the
scopes that claim it, an hourly upload count per scope, and running totals
kept by triggers (the global cap check is a primary-key read)."""

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
    "INSERT INTO gfs_space_media(blob_id, size, created_at, expires_at,"
    " last_fetched_at, owner_kind, owner_id) VALUES(?, ?, 10, 20, 10, 'space', 's1')"
)
_CLAIM = (
    "INSERT INTO gfs_space_media_claims(blob_id, scope_kind, scope_id, size,"
    " created_at, expires_at) VALUES(?, ?, ?, 100, 10, 20)"
)


def _plan(conn, sql: str) -> str:
    return " ".join(str(r[-1]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql))


def test_one_row_per_blob_with_a_positive_size(conn):
    conn.execute(_BLOB, ("a" * 64, 65_564))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_BLOB, ("a" * 64, 65_564))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_BLOB, ("b" * 64, 0))


def test_a_blob_may_be_claimed_by_several_scopes_once_each(conn):
    conn.execute(_CLAIM, ("a" * 64, "space", "s1"))
    conn.execute(_CLAIM, ("a" * 64, "space", "s2"))
    conn.execute(_CLAIM, ("a" * 64, "channel", "s1"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_CLAIM, ("a" * 64, "space", "s1"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_CLAIM, ("b" * 64, "room", "x"))


def test_totals_follow_inserts_and_deletes(conn):
    def totals():
        return conn.execute(
            "SELECT blobs, bytes FROM gfs_space_media_totals WHERE id=1"
        ).fetchone()

    assert totals() == (0, 0)
    conn.execute(_BLOB, ("a" * 64, 100))
    conn.execute(_BLOB, ("b" * 64, 300))
    assert totals() == (2, 400)
    conn.execute("DELETE FROM gfs_space_media WHERE blob_id=?", ("a" * 64,))
    assert totals() == (1, 300)


def test_upload_counts_are_one_row_per_scope_and_hour(conn):
    ins = (
        "INSERT INTO gfs_space_media_uploads(scope_kind, scope_id, hour_bucket,"
        " count) VALUES('space', 's1', 7, 1)"
    )
    conn.execute(ins)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins)


def test_the_hot_reads_use_their_indexes(conn):
    assert "idx_gfs_space_media_claims_scope" in _plan(
        conn,
        "SELECT SUM(size) FROM gfs_space_media_claims"
        " WHERE scope_kind='space' AND scope_id='s1' AND expires_at > 5",
    )
    assert "idx_gfs_space_media_lru" in _plan(
        conn,
        "SELECT blob_id FROM gfs_space_media"
        " ORDER BY last_fetched_at, created_at LIMIT 10",
    )
    assert "idx_gfs_space_media_expires" in _plan(
        conn, "SELECT blob_id FROM gfs_space_media WHERE expires_at <= 5"
    )
    assert "idx_gfs_space_media_uploads_bucket" in _plan(
        conn, "DELETE FROM gfs_space_media_uploads WHERE hour_bucket < 5"
    )


def test_one_sweep_lease_row(conn):
    assert conn.execute(
        "SELECT holder, lease_until, cursor FROM gfs_space_media_sweep"
    ).fetchall() == [("", 0, "")]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO gfs_space_media_sweep(id) VALUES(2)")


def test_claim_sequence_never_reuses_a_number(conn):
    """An eviction's mark must never cover a claim made after it: the claim
    sequence only grows, even after the newest claim was deleted."""
    conn.execute(_CLAIM, ("a" * 64, "space", "s1"))
    conn.execute(_CLAIM, ("b" * 64, "space", "s1"))
    top = conn.execute("SELECT MAX(seq) FROM gfs_space_media_claims").fetchone()[0]
    conn.execute("DELETE FROM gfs_space_media_claims WHERE seq=?", (top,))
    conn.execute(_CLAIM, ("c" * 64, "space", "s1"))
    assert (
        conn.execute(
            "SELECT seq FROM gfs_space_media_claims WHERE blob_id=?", ("c" * 64,)
        ).fetchone()[0]
        > top
    )


def test_a_blob_has_an_owner_scope(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO gfs_space_media(blob_id, size, created_at, expires_at,"
            " last_fetched_at, owner_kind, owner_id)"
            " VALUES(?, 100, 10, 20, 10, 'room', 's1')",
            ("a" * 64,),
        )


def test_tombstones_are_one_per_blob_and_swept_by_time(conn):
    conn.execute(
        "INSERT INTO gfs_space_media_tombstones(blob_id, until) VALUES(?, 5)",
        ("a" * 64,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO gfs_space_media_tombstones(blob_id, until) VALUES(?, 6)",
            ("a" * 64,),
        )
    assert "idx_gfs_space_media_tombstones_until" in _plan(
        conn, "SELECT blob_id FROM gfs_space_media_tombstones WHERE until <= 10"
    )
