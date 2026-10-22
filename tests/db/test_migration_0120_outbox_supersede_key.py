"""Migration 0120 — ``federation_outbox.supersede_key`` for latest-only
state rows (online status, space-map pins)."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 120


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    apply_through(c, _VERSION - 1)
    c.execute(
        "INSERT INTO federation_outbox(id, instance_id, event_type, payload_json)"
        " VALUES('old', 'peer', 'dm_message', '{}')"
    )
    apply_through(c, _VERSION)
    yield c
    c.close()


def test_an_existing_row_is_kept_unkeyed(conn):
    row = conn.execute(
        "SELECT supersede_key, status FROM federation_outbox WHERE id='old'"
    ).fetchone()
    assert row["supersede_key"] is None
    assert row["status"] == "pending"


def test_a_new_row_can_carry_a_key(conn):
    conn.execute(
        "INSERT INTO federation_outbox(id, instance_id, event_type, payload_json,"
        " supersede_key) VALUES('new', 'peer', 'user_online', '{}', 'online:u1')"
    )
    assert (
        conn.execute(
            "SELECT supersede_key FROM federation_outbox WHERE id='new'"
        ).fetchone()["supersede_key"]
        == "online:u1"
    )


def test_the_lookup_uses_the_partial_index(conn):
    plan = " ".join(
        r["detail"]
        for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM federation_outbox"
            " WHERE instance_id='peer' AND supersede_key='online:u1'"
            " AND status='pending'"
        )
    )
    assert "idx_federation_outbox_supersede" in plan
