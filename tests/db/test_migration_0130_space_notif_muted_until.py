"""Migration 0130 — ``space_notif_prefs`` learns ``muted_until``: one
NULL-defaulted column, every existing row (a forever mute included)
untouched."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 130


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
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('s1', 'S', 'i', 'anna', ?)",
        ("ab" * 32,),
    )
    c.execute(
        "INSERT INTO space_notif_prefs(user_id, space_id, level)"
        " VALUES('u-a', 's1', 'muted')"
    )
    before = [tuple(r) for r in c.execute("SELECT * FROM space_notif_prefs")]
    apply_through(c, _VERSION)
    after = [tuple(r) for r in c.execute("SELECT * FROM space_notif_prefs")]
    assert [r[: len(before[0])] for r in after] == before
    assert [r[len(before[0]) :] for r in after] == [(None,)]
    yield c
    c.close()


def test_a_timed_mute_keeps_the_level_to_return_to(conn):
    conn.execute(
        "UPDATE space_notif_prefs SET level='mentions',"
        " muted_until='2026-10-28T02:15:00+00:00' WHERE user_id='u-a'"
    )
    row = conn.execute("SELECT level, muted_until FROM space_notif_prefs").fetchone()
    assert tuple(row) == ("mentions", "2026-10-28T02:15:00+00:00")


def test_the_level_check_still_holds(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE space_notif_prefs SET level='later'")
