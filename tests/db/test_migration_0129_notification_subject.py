"""Migration 0129 — ``notifications`` learns its object, space and params:
three NULL-defaulted columns, every existing row untouched, ``params`` holds
JSON only, and the object lookups ride the new partial index."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 129


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
        "INSERT INTO users(username, user_id, display_name) VALUES('anna', 'u-a', 'Anna')"
    )
    c.execute(
        "INSERT INTO notifications(id, user_id, type, title, link_url)"
        " VALUES('n1', 'u-a', 'post_created', 'Ben posted', '/post/p1')"
    )
    before = [tuple(r) for r in c.execute("SELECT * FROM notifications")]
    apply_through(c, _VERSION)
    after = [tuple(r) for r in c.execute("SELECT * FROM notifications")]
    # The old columns keep their values; the three new ones are NULL.
    assert [r[: len(before[0])] for r in after] == before
    assert [r[len(before[0]) :] for r in after] == [(None, None, None)]
    yield c
    c.close()


def test_new_rows_carry_object_space_and_params(conn):
    conn.execute(
        "INSERT INTO notifications(id, user_id, type, title, object_key,"
        " space_id, params) VALUES('n2', 'u-a', 'space_post_created', 't',"
        " 'space_post:p2', 's1', '{\"new\": 1, \"total\": 1}')"
    )
    row = conn.execute(
        "SELECT object_key, space_id, params FROM notifications WHERE id='n2'"
    ).fetchone()
    assert tuple(row) == ("space_post:p2", "s1", '{"new": 1, "total": 1}')


def test_params_must_be_json(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO notifications(id, user_id, type, title, params)"
            " VALUES('n3', 'u-a', 't', 't', 'not json')"
        )


@pytest.mark.parametrize(
    "where",
    [
        "object_key = ? AND user_id = 'u-a' AND type = 'x'",
        "object_key = ?",
    ],
    ids=["group-lookup", "by-object"],
)
def test_object_lookups_use_the_partial_index(conn, where):
    plan = " ".join(
        str(r[3])
        for r in conn.execute(
            f"EXPLAIN QUERY PLAN SELECT id FROM notifications WHERE {where}",
            ("post:p1",),
        )
    )
    assert "idx_notifications_object" in plan, plan
