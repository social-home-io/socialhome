"""Migration 0112 — a public-moment follow keeps the author's bio and the
digest of their GFS-mirrored picture (both NULL until known)."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 112


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
        "INSERT INTO users(user_id, username, display_name, state)"
        " VALUES('u1', 'alice', 'Alice', 'active')"
    )
    c.execute(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name,"
        " public_key, inbox_url, status, paired_at) VALUES('g1', 'gfs-1', 'G',"
        " 'aa', 'https://g.example', 'active', datetime('now'))"
    )
    c.execute(
        "INSERT INTO moment_public_follows(follower_user_id, followed_user_id,"
        " gfs_id, followed_instance_pk, followed_username, followed_display_name)"
        " VALUES('u1', 'u-noor', 'g1', 'bb', 'noor', 'Noor')"
    )
    apply_through(c, _VERSION)
    yield c
    c.close()


def test_an_existing_follow_survives_with_no_profile(conn):
    row = conn.execute(
        "SELECT followed_display_name, followed_bio, followed_picture_digest"
        " FROM moment_public_follows"
    ).fetchone()
    assert tuple(row) == ("Noor", None, None)


def test_bio_and_picture_digest_are_stored(conn):
    conn.execute(
        "UPDATE moment_public_follows SET followed_bio='Beekeeper.',"
        " followed_picture_digest='0123456789abcdef'"
    )
    row = conn.execute(
        "SELECT followed_bio, followed_picture_digest FROM moment_public_follows"
    ).fetchone()
    assert tuple(row) == ("Beekeeper.", "0123456789abcdef")
