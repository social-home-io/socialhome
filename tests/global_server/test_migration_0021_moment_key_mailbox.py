"""GFS migration 0021 — encrypted public moments (v_62): a follow keeps the
suites its household declared (NULL for older follows), and the mailbox
holds at most one sealed key per (author, follower household), cascading
with the registration and the household."""

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
    apply_through(c, 20, directory=_GFS_MIGRATIONS)
    c.execute(
        "INSERT INTO client_instances(instance_id, display_name, public_key,"
        " status) VALUES('inst-f', 'F', 'aa', 'active'), ('inst-a', 'A', 'cc', 'active')"
    )
    c.execute(
        "INSERT INTO gfs_user_registrations(user_id, instance_id, username,"
        " display_name, home_instance_pk, registered_at, status)"
        " VALUES('u-a', 'inst-a', 'a', 'A', 'bb', 1, 'active')"
    )
    c.execute(
        "INSERT INTO gfs_moment_follows(follower_user_id, follower_instance_id,"
        " followed_user_id) VALUES('u-f', 'inst-f', 'u-a')"
    )
    apply_through(c, 21, directory=_GFS_MIGRATIONS)
    yield c
    c.close()


def test_an_older_follow_declares_no_suites(conn):
    row = conn.execute("SELECT moment_suites FROM gfs_moment_follows").fetchone()
    assert row[0] is None


def test_one_mailbox_row_per_author_and_household_cascading(conn):
    ins = (
        "INSERT INTO gfs_moment_key_mailbox(author_user_id, follower_instance_id,"
        " key_gen, key_step, delivery) VALUES('u-a', 'inst-f', 1, 0, '{}')"
    )
    conn.execute(ins)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins)
    assert conn.execute("SELECT delivered FROM gfs_moment_key_mailbox").fetchone() == (
        0,
    )
    conn.execute("DELETE FROM gfs_user_registrations WHERE user_id='u-a'")
    assert conn.execute("SELECT COUNT(*) FROM gfs_moment_key_mailbox").fetchone() == (
        0,
    )
