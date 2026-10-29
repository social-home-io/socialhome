"""Migration 0131 — ``app_pending_sessions`` learns the mailbox columns:
``held_json`` / ``updated_at`` NULL-defaulted, ``state`` defaulting to
'invited' (what every existing row is); no existing row rewritten."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 131


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
    assert [r[: len(before[0])] for r in after] == before
    yield c
    c.close()


def test_an_existing_invite_reads_as_invited_with_nothing_held(conn):
    row = conn.execute(
        "SELECT held_json, state, updated_at FROM app_pending_sessions"
    ).fetchone()
    assert tuple(row) == (None, "invited", None)


def test_a_game_becomes_active_and_holds_a_message(conn):
    conn.execute(
        "UPDATE app_pending_sessions SET state='active',"
        ' held_json=\'{"kind":"message","payload":{}}\','
        " updated_at='2026-10-29T10:00:00+00:00'"
    )
    row = conn.execute("SELECT state, held_json FROM app_pending_sessions").fetchone()
    assert row["state"] == "active"


def test_the_state_check_holds(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE app_pending_sessions SET state='declined'")


def test_uninstall_still_drops_the_mailbox(conn):
    conn.execute("DELETE FROM installed_apps WHERE app_id='chess'")
    assert conn.execute("SELECT COUNT(*) FROM app_pending_sessions").fetchone()[0] == 0
