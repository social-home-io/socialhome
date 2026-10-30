"""Migration 0135 — ``bazaar_listing_faq`` (a listing's questions and answers).

Additive: a new table, no existing row changes. The FAQ goes with its
listing, and a change to it re-stamps the wrapper post for the space sync.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 135


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
    apply_through(c, _VERSION)
    yield c
    c.close()


def _seq(c: sqlite3.Connection) -> int | None:
    return c.execute("SELECT sync_seq FROM space_posts WHERE id='post-1'").fetchone()[0]


def test_an_existing_listing_has_no_questions(conn):
    assert conn.execute("SELECT COUNT(*) FROM bazaar_listing_faq").fetchone()[0] == 0


def test_an_entry_belongs_to_a_listing_and_goes_with_it(conn):
    conn.execute(
        "INSERT INTO bazaar_listing_faq(listing_post_id, entry_id, question,"
        " answer, rev) VALUES('post-1', 'e1', 'Q', 'A', 1)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO bazaar_listing_faq(listing_post_id, entry_id, question,"
            " answer, rev) VALUES('no-such-listing', 'e2', 'Q', 'A', 1)"
        )
    conn.execute("DELETE FROM bazaar_listings WHERE post_id='post-1'")
    assert conn.execute("SELECT COUNT(*) FROM bazaar_listing_faq").fetchone()[0] == 0


@pytest.mark.parametrize(("rev", "removed"), [(0, 0), (1, 2)])
def test_rev_and_removed_are_checked(conn, rev, removed):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO bazaar_listing_faq(listing_post_id, entry_id, rev, removed)"
            " VALUES('post-1', 'e1', ?, ?)",
            (rev, removed),
        )


def test_a_change_restamps_the_wrapper_post(conn):
    start = _seq(conn)
    conn.execute(
        "INSERT INTO bazaar_listing_faq(listing_post_id, entry_id, question,"
        " answer, rev) VALUES('post-1', 'e1', 'Q', 'A', 1)"
    )
    after_insert = _seq(conn)
    assert after_insert is not None and after_insert != start
    # Local bookkeeping alone stamps nothing.
    conn.execute("UPDATE bazaar_listing_faq SET updated_at='2030-01-01 00:00:00'")
    assert _seq(conn) == after_insert
    conn.execute("UPDATE bazaar_listing_faq SET answer='B', rev=2")
    after_edit = _seq(conn)
    assert after_edit > after_insert
    conn.execute("DELETE FROM bazaar_listing_faq")
    assert _seq(conn) > after_edit
