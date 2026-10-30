"""Migration 0133 — ``bazaar_offers`` learns ``rejection_reason``: one
NULL-defaulted column, every existing offer untouched."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 133


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    apply_through(c, _VERSION - 1)
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('s1', 'S', 'i', 'anna', ?)",
        ("ab" * 32,),
    )
    c.execute(
        "INSERT INTO space_posts(id, space_id, author, type, content)"
        " VALUES('p1', 's1', 'u-a', 'bazaar', '')"
    )
    c.execute(
        "INSERT INTO bazaar_listings(post_id, space_id, seller_user_id, mode,"
        " title, end_time, currency, status, price)"
        " VALUES('p1', 's1', 'u-a', 'negotiable', 'Bike',"
        " '2026-11-06T00:00:00+00:00', 'EUR', 'active', 100)"
    )
    c.execute(
        "INSERT INTO bazaar_offers(id, listing_post_id, offerer_user_id, amount,"
        " message, status) VALUES('o1', 'p1', 'u-b', 90, 'hi', 'rejected')"
    )
    before = [tuple(r) for r in c.execute("SELECT * FROM bazaar_offers")]
    apply_through(c, _VERSION)
    after = [tuple(r) for r in c.execute("SELECT * FROM bazaar_offers")]
    assert [r[: len(before[0])] for r in after] == before
    assert [r[len(before[0]) :] for r in after] == [(None,)]
    yield c
    c.close()


def test_a_declined_offer_keeps_the_sellers_reason(conn):
    conn.execute("UPDATE bazaar_offers SET rejection_reason='too low' WHERE id='o1'")
    row = conn.execute("SELECT status, rejection_reason FROM bazaar_offers").fetchone()
    assert tuple(row) == ("rejected", "too low")


def test_the_status_check_still_holds(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE bazaar_offers SET status='later'")
