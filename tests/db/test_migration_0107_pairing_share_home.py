"""Migration 0107 — pairing honours the household's home-sharing choice.

Adds the household's ``share_home_default`` (on) and the pairing session's
``share_home`` (off for a session minted before it — fail-closed).
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 107


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
    c.execute("INSERT OR IGNORE INTO preferences(id) VALUES('household')")
    c.execute(
        "INSERT INTO pending_pairings(token, own_identity_pk, own_dh_pk,"
        " own_dh_sk, inbox_url, own_local_inbox_id, expires_at)"
        " VALUES('old', 'aa', 'bb', 'cc', 'https://x/inbox/i', 'i', '2099-01-01')"
    )
    apply_through(c, _VERSION)
    yield c
    c.close()


def test_household_default_is_on(conn):
    row = conn.execute(
        "SELECT share_home_default FROM preferences WHERE id='household'"
    ).fetchone()
    assert row["share_home_default"] == 1


def test_a_session_minted_before_shares_nothing(conn):
    row = conn.execute(
        "SELECT share_home FROM pending_pairings WHERE token='old'"
    ).fetchone()
    assert row["share_home"] == 0


def test_a_new_session_defaults_to_not_sharing(conn):
    conn.execute(
        "INSERT INTO pending_pairings(token, own_identity_pk, own_dh_pk,"
        " own_dh_sk, inbox_url, own_local_inbox_id, expires_at)"
        " VALUES('new', 'aa', 'bb', 'cc', 'https://x/inbox/j', 'j', '2099-01-01')"
    )
    row = conn.execute(
        "SELECT share_home FROM pending_pairings WHERE token='new'"
    ).fetchone()
    assert row["share_home"] == 0
