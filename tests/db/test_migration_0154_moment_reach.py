"""Migration 0154 — ``moments.reach``: how many hops a moment goes (v_96).

Additive: one NULL column (NULL = 3, every existing row), no backfill, no
index.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 154


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
        "INSERT INTO moments(id, author_user_id, content, origin_instance_id,"
        " expires_at) VALUES('m-old', 'u', 'hi', 'o', '2099-01-01')"
    )
    apply_through(c, _VERSION)
    yield c
    c.close()


def test_existing_rows_keep_a_null_reach(db):
    row = dict(db.execute("SELECT * FROM moments WHERE id='m-old'").fetchone())
    assert row["reach"] is None
    assert row["content"] == "hi"


def test_new_rows_take_a_reach(db):
    db.execute(
        "INSERT INTO moments(id, author_user_id, content, origin_instance_id,"
        " expires_at, reach) VALUES('m-new', 'u', '', 'o', '2099-01-01', 2)"
    )
    row = db.execute("SELECT reach FROM moments WHERE id='m-new'").fetchone()
    assert row["reach"] == 2
