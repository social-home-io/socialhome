"""Migration 0136 — group sessions: ``app_group_sessions`` (one row per
table a household knows) and ``app_group_held`` (the per-seat mailbox,
cascading with the person's session row). Additive: no existing row
touched."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 136

_TABLE = (
    "INSERT INTO app_group_sessions(session_id, app_id, host_instance,"
    " host_user, hub_instance, state, min_players, max_players, roster_json,"
    " roster_rev, created_at, expires_at, updated_at)"
    " VALUES(?, 'chess', 'hub', 'anna', 'hub', ?, ?, ?, '[]', 1,"
    " '2026-10-31T10:00:00+00:00', '2026-11-03T10:00:00+00:00',"
    " '2026-10-31T10:00:00+00:00')"
)


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
        "INSERT INTO installed_apps(app_id, name, version, enabled, manifest_json,"
        " bundle_path, bundle_sha256, source_url, installed_at)"
        " VALUES('chess', 'Chess', '1', 1, '{}', '/b', 'h', 'u', '2026-10-01')"
    )
    c.execute(
        "INSERT INTO app_pending_sessions(app_id, user_id, session_id,"
        " from_instance, from_user, payload_json, created_at)"
        " VALUES('chess', 'u-a', 's1', 'peer', 'ben', '{\"verb\":\"open\"}',"
        " '2026-10-28T10:00:00+00:00')"
    )
    before = [tuple(r) for r in c.execute("SELECT * FROM app_pending_sessions")]
    apply_through(c, _VERSION)
    after = [tuple(r) for r in c.execute("SELECT * FROM app_pending_sessions")]
    assert after == before
    yield c
    c.close()


def test_a_table_is_kept_with_its_defaults(conn):
    conn.execute(_TABLE, ("t1", "lobby", 2, 4))
    row = conn.execute("SELECT * FROM app_group_sessions").fetchone()
    assert row["seq"] == 0
    assert row["late_join"] == 0
    assert row["space_id"] is None
    assert row["seat_key_json"] is None


@pytest.mark.parametrize(
    ("state", "lo", "hi"),
    [("waiting", 2, 4), ("lobby", 1, 4), ("lobby", 4, 3), ("lobby", 2, 31)],
)
def test_the_checks_hold(conn, state, lo, hi):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_TABLE, ("t1", state, lo, hi))


def test_a_table_needs_no_installed_app(conn):
    conn.execute("DELETE FROM installed_apps WHERE app_id='chess'")
    conn.execute(_TABLE, ("t1", "lobby", 2, 30))
    assert conn.execute("SELECT COUNT(*) FROM app_group_sessions").fetchone()[0] == 1


def test_held_goes_with_the_session_row(conn):
    conn.execute(
        "INSERT INTO app_group_held(app_id, user_id, session_id, from_seat, seq,"
        " held_json, updated_at) VALUES('chess', 'u-a', 's1', 2, 7, '{}',"
        " '2026-10-31T10:00:00+00:00')"
    )
    conn.execute("DELETE FROM installed_apps WHERE app_id='chess'")
    assert conn.execute("SELECT COUNT(*) FROM app_group_held").fetchone()[0] == 0


def test_held_needs_a_session_row(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO app_group_held(app_id, user_id, session_id, from_seat,"
            " seq, held_json, updated_at) VALUES('chess', 'u-a', 'nope', 2, 7,"
            " '{}', '2026-10-31T10:00:00+00:00')"
        )
