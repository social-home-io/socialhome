"""Migration 0134 — ``conversations.bazaar_listing_id`` ("Ask the seller").

Additive: every existing conversation stays a plain DM / group / system chat
(``NULL``); a listing thread names its listing and goes with it.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 134


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
    c.execute("INSERT INTO conversations(id, type) VALUES('old-dm', 'dm')")
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp','S','host','anna','ab')"
    )
    c.execute(
        "INSERT INTO space_posts(id, space_id, author, type, content)"
        " VALUES('post-1','sp','u-sam','bazaar','Bike')"
    )
    c.execute(
        "INSERT INTO bazaar_listings(post_id, space_id, seller_user_id, mode,"
        " title, end_time, currency) VALUES('post-1','sp','u-sam','fixed',"
        " 'Bike','2099-01-01T00:00:00+00:00','EUR')"
    )
    yield c
    c.close()


def test_existing_conversations_are_no_listing_thread(conn):
    apply_through(conn, _VERSION)
    row = conn.execute(
        "SELECT bazaar_listing_id FROM conversations WHERE id='old-dm'"
    ).fetchone()
    assert row["bazaar_listing_id"] is None


def test_a_thread_names_its_listing_and_goes_with_it(conn):
    apply_through(conn, _VERSION)
    conn.execute(
        "INSERT INTO conversations(id, type, bazaar_listing_id)"
        " VALUES('t-1', 'dm', 'post-1')"
    )
    conn.execute("DELETE FROM bazaar_listings WHERE post_id='post-1'")
    assert (
        conn.execute("SELECT id FROM conversations WHERE id='t-1'").fetchone() is None
    )
    assert conn.execute("SELECT id FROM conversations WHERE id='old-dm'").fetchone()


def test_a_thread_must_name_a_held_listing(conn):
    apply_through(conn, _VERSION)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO conversations(id, type, bazaar_listing_id)"
            " VALUES('t-2', 'dm', 'no-such-listing')"
        )


def test_the_listing_index_covers_threads_only(conn):
    apply_through(conn, _VERSION)
    idx = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='idx_conversations_bazaar_listing'"
    ).fetchone()
    assert idx is not None and "bazaar_listing_id IS NOT NULL" in idx["sql"]
