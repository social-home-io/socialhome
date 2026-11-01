"""Migration 0138 — app engine state (``app_engine_state``, one KEK-wrapped
blob per table and engine, v_78). Additive: existing tables keep their
values and hold no engine state."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 138


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
        "INSERT INTO app_group_sessions(session_id, app_id, host_instance,"
        " host_user, hub_instance, state, min_players, max_players, roster_json,"
        " roster_rev, created_at, expires_at, updated_at)"
        " VALUES('t1', 'gift-draw', 'hub', 'anna', 'hub', 'lobby', 3, 30, '[]', 1,"
        " '2026-10-31T10:00:00+00:00', '2026-11-03T10:00:00+00:00',"
        " '2026-10-31T10:00:00+00:00')"
    )
    before = [tuple(r) for r in c.execute("SELECT * FROM app_group_sessions")]
    apply_through(c, _VERSION)
    after = [
        tuple(r)[: len(before[0])]
        for r in c.execute("SELECT * FROM app_group_sessions")
    ]
    assert after == before
    yield c
    c.close()


def test_existing_tables_hold_no_engine_state(conn):
    assert conn.execute("SELECT COUNT(*) FROM app_engine_state").fetchone()[0] == 0


def test_engine_state_is_keyed_by_table_and_engine_and_goes_with_its_table(conn):
    conn.execute(
        "INSERT INTO app_engine_state(session_id, engine, state_json, updated_at)"
        " VALUES('t1', 'draw', '{}', '2026-11-01T00:00:00+00:00')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO app_engine_state(session_id, engine, state_json, updated_at)"
            " VALUES('t1', 'draw', '{}', '2026-11-01T00:00:00+00:00')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO app_engine_state(session_id, engine, state_json, updated_at)"
            " VALUES('nope', 'draw', '{}', '2026-11-01T00:00:00+00:00')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO app_engine_state(session_id, engine, state_json, updated_at)"
            " VALUES('t1', '', '{}', '2026-11-01T00:00:00+00:00')"
        )
    conn.execute("DELETE FROM app_group_sessions WHERE session_id = 't1'")
    assert conn.execute("SELECT COUNT(*) FROM app_engine_state").fetchone()[0] == 0
