"""Migration 0137 — a space's games list (``spaces.allowed_apps``) and the
card a household keeps of a space table it has nobody at
(``app_group_sessions.card_json``). Additive: existing rows keep their
values and read "no games"."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 137


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
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp1', 'Lakeside', 'hub', 'anna', 'aa')"
    )
    c.execute(
        "INSERT INTO app_group_sessions(session_id, app_id, host_instance,"
        " host_user, hub_instance, state, min_players, max_players, roster_json,"
        " roster_rev, created_at, expires_at, updated_at)"
        " VALUES('t1', 'chess', 'hub', 'anna', 'hub', 'lobby', 2, 2, '[]', 1,"
        " '2026-10-31T10:00:00+00:00', '2026-11-03T10:00:00+00:00',"
        " '2026-10-31T10:00:00+00:00')"
    )
    before = [tuple(r) for r in c.execute("SELECT * FROM spaces")]
    apply_through(c, _VERSION)
    after = [tuple(r)[: len(before[0])] for r in c.execute("SELECT * FROM spaces")]
    assert after == before
    yield c
    c.close()


def test_existing_spaces_have_no_games(conn):
    row = conn.execute("SELECT allowed_apps FROM spaces WHERE id='sp1'").fetchone()
    assert row["allowed_apps"] == "[]"


def test_existing_tables_have_no_card(conn):
    row = conn.execute("SELECT card_json FROM app_group_sessions").fetchone()
    assert row["card_json"] is None


def test_the_list_is_never_null(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE spaces SET allowed_apps = NULL WHERE id = 'sp1'")
