"""Migration 0152 — ``idx_space_posts_sync_keyset ON space_posts(space_id,
deleted)``: the §25.6 sync pages of a space seek newest-first on rowid.

Additive: one index, no row touched.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 152


@pytest.fixture
def before(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    apply_through(c, _VERSION - 1)
    yield c
    c.close()


def _seed(c: sqlite3.Connection) -> None:
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp', 'S', 'i', 'u', 'k')"
    )
    c.executemany(
        "INSERT INTO space_posts(id, space_id, author, type, deleted)"
        " VALUES(?, 'sp', 'u', 'text', ?)",
        [(f"p{i}", int(i % 3 == 0)) for i in range(9)],
    )


def test_the_index_covers_space_and_deleted_and_keeps_every_row(before):
    _seed(before)
    rows = before.execute("SELECT rowid, * FROM space_posts ORDER BY rowid").fetchall()
    apply_through(before, _VERSION)
    cols = [
        r["name"]
        for r in before.execute("PRAGMA index_info('idx_space_posts_sync_keyset')")
    ]
    assert cols == ["space_id", "deleted"]
    after = before.execute("SELECT rowid, * FROM space_posts ORDER BY rowid").fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in rows]


def test_a_sync_page_seeks_on_the_index_without_a_sort(before):
    apply_through(before, _VERSION)
    _seed(before)
    plan = " | ".join(
        r[3]
        for r in before.execute(
            "EXPLAIN QUERY PLAN SELECT rowid FROM space_posts"
            " WHERE space_id=? AND deleted=0 AND rowid <= ?"
            " ORDER BY rowid DESC LIMIT 200",
            ("sp", 2**63 - 1),
        )
    )
    assert "idx_space_posts_sync_keyset (space_id=? AND deleted=? AND rowid<?)" in plan
    assert "TEMP B-TREE" not in plan
