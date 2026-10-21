"""GFS migration 0022 — encrypted moment media (v_63): one metadata row per
blob, addressed by its SHA-256, cascading with the author's registration."""

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
    apply_through(c, 22, directory=_GFS_MIGRATIONS)
    c.execute(
        "INSERT INTO client_instances(instance_id, display_name, public_key,"
        " status) VALUES('inst-a', 'A', 'cc', 'active')"
    )
    c.execute(
        "INSERT INTO gfs_user_registrations(user_id, instance_id, username,"
        " display_name, home_instance_pk, registered_at, status)"
        " VALUES('u-a', 'inst-a', 'a', 'A', 'bb', 1, 'active')"
    )
    yield c
    c.close()


def test_one_row_per_blob_and_it_cascades_with_the_author(conn):
    ins = (
        "INSERT INTO gfs_moment_media(blob_id, author_user_id, size, expires_at)"
        " VALUES(?, 'u-a', ?, 100)"
    )
    conn.execute(ins, ("a" * 64, 65_564))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("a" * 64, 65_564))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("b" * 64, 0))
    assert conn.execute("SELECT created_at FROM gfs_moment_media").fetchone()[0] > 0
    conn.execute("DELETE FROM gfs_user_registrations WHERE user_id='u-a'")
    assert conn.execute("SELECT COUNT(*) FROM gfs_moment_media").fetchone()[0] == 0
