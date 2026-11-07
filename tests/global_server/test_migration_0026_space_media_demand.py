"""GFS migration 0026 — demand for space blobs that left the cache (v_83):
one row per (blob, scope), with when the cache stored the blob (NULL when
it never did here), a request count in a window, and the cluster-wide
re-upload claim."""

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
    apply_through(c, 26, directory=_GFS_MIGRATIONS)
    yield c
    c.close()


def test_table_shape(conn):
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(gfs_space_media_demand)")}
    assert set(cols) == {
        "blob_id",
        "scope_kind",
        "scope_id",
        "stored_at",
        "window_start",
        "requests",
        "claimed_until",
        "touched_at",
    }
    # stored_at is NULL for a blob never stored here.
    assert cols["stored_at"][3] == 0


def test_one_row_per_blob_and_scope(conn):
    ins = (
        "INSERT INTO gfs_space_media_demand(blob_id, scope_kind, scope_id,"
        " touched_at) VALUES(?,?,?,1)"
    )
    conn.execute(ins, ("a" * 64, "space", "s1"))
    # A reader naming another scope for the same id gets its own row: it
    # can't touch the real scope's counts or claim.
    conn.execute(ins, ("a" * 64, "channel", "c1"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("a" * 64, "space", "s1"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("b" * 64, "galaxy", "s1"))


def test_touched_index_for_the_prune(conn):
    idx = {r[1] for r in conn.execute("PRAGMA index_list(gfs_space_media_demand)")}
    assert "idx_gfs_space_media_demand_touched" in idx
