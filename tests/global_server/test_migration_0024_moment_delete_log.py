"""GFS migration 0024 — signed public-moment delete certificates (v_66): one
row per (author, moment), cascading with the author's registration."""

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
    apply_through(c, 24, directory=_GFS_MIGRATIONS)
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


def test_one_row_per_moment_and_it_cascades_with_the_author(conn):
    ins = (
        "INSERT INTO gfs_moment_delete_log(author_user_id, moment_id, cert)"
        " VALUES('u-a', ?, '{}')"
    )
    conn.execute(ins, ("m1",))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("m1",))
    conn.execute(ins, ("m2",))
    first = conn.execute("SELECT MIN(created_at) FROM gfs_moment_delete_log")
    assert first.fetchone()[0] > 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO gfs_moment_delete_log(author_user_id, moment_id, cert)"
            " VALUES('nobody', 'm3', '{}')"
        )
    conn.execute("DELETE FROM gfs_user_registrations WHERE user_id='u-a'")
    left = conn.execute("SELECT COUNT(*) FROM gfs_moment_delete_log")
    assert left.fetchone()[0] == 0


def test_the_prune_and_author_reads_have_an_index(conn):
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name='gfs_moment_delete_log'"
        )
    }
    assert {
        "idx_gfs_moment_delete_log_author",
        "idx_gfs_moment_delete_log_created",
    } <= names
