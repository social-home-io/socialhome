"""Migration 0153 — both discovery caches carry a space's declared member and
follower counts; "not declared" is NULL (no number), never 0."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 153


@pytest.fixture
def db(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    apply_through(c, _VERSION - 1)
    c.execute(
        "INSERT INTO public_space_cache(space_id, instance_id, name, description,"
        " member_count, min_age, category, join_mode, allow_subscribers)"
        " VALUES('g1', 'host', 'Chess', 'd', 0, 13, 'games', 'open', 1)"
    )
    c.execute(
        "INSERT INTO peer_space_directory(instance_id, space_id, name,"
        " member_count) VALUES('peer', 'p1', 'Garden', 4)"
    )
    apply_through(c, _VERSION)
    yield c
    c.close()


def test_cached_global_rows_survive_with_no_number(db):
    """The stored count was the GFS's relay seats, not the space's members:
    the row keeps everything else and shows no number until the next poll."""
    row = dict(db.execute("SELECT * FROM public_space_cache").fetchone())
    assert row["member_count"] is None
    assert row["follower_count"] is None
    assert (row["name"], row["min_age"], row["category"]) == ("Chess", 13, "games")
    assert (row["join_mode"], row["allow_subscribers"]) == ("open", 1)


def test_global_rows_take_declared_counts_or_null(db):
    db.execute(
        "INSERT INTO public_space_cache(space_id, instance_id, name,"
        " member_count, follower_count) VALUES('g2', 'h', 'n', 5, 12)"
    )
    db.execute(
        "INSERT INTO public_space_cache(space_id, instance_id, name)"
        " VALUES('g3', 'h', 'n')"
    )
    rows = {
        r["space_id"]: (r["member_count"], r["follower_count"])
        for r in db.execute("SELECT * FROM public_space_cache")
    }
    assert rows["g2"] == (5, 12)
    assert rows["g3"] == (None, None)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO public_space_cache(space_id, instance_id, name,"
            " member_count) VALUES('bad', 'h', 'n', -1)"
        )


def test_peer_rows_gain_a_nullable_follower_count(db):
    row = db.execute("SELECT * FROM peer_space_directory").fetchone()
    assert (row["member_count"], row["follower_count"]) == (4, None)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO peer_space_directory(instance_id, space_id, name,"
            " follower_count) VALUES('peer', 'p2', 'n', -3)"
        )
