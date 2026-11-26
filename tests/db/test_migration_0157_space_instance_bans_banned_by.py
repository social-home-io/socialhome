"""Migration 0157 — ``space_instance_bans`` gains a NULL-defaulted
``banned_by`` (who banned the household, P7b); existing rows keep NULL."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 157


@pytest.fixture
def db(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    apply_through(c, _VERSION - 1)
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('s1', 'S', 'i', 'o', 'aa')"
    )
    c.execute(
        "INSERT INTO space_instance_bans(space_id, instance_id) VALUES('s1', 'h1')"
    )
    yield c
    c.close()


def test_banned_by_is_added_and_old_rows_keep_null(db):
    apply_through(db, _VERSION)
    cols = [r[1] for r in db.execute("PRAGMA table_info(space_instance_bans)")]
    assert "banned_by" in cols
    assert db.execute(
        "SELECT banned_by FROM space_instance_bans WHERE instance_id='h1'"
    ).fetchone() == (None,)
