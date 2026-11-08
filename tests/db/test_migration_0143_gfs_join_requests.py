"""Migration 0143 — "Ask to join" through the GFS (v_84): four NULL-defaulted
columns; existing rows are untouched."""

from __future__ import annotations

import sqlite3

from tests.db.connections import connect
from tests.migration_chain import apply_through

_VERSION = 143


def _fresh(tmp_path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _rows(c: sqlite3.Connection) -> None:
    c.execute(
        "INSERT OR IGNORE INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-1', 'S', 'inst', 'admin', 'aa')"
    )
    c.execute(
        "INSERT INTO space_join_requests(id, space_id, user_id, expires_at)"
        " VALUES('r-old', 'sp-1', 'u-1', '2099-01-01T00:00:00+00:00')"
    )
    c.execute(
        "INSERT INTO space_invite_tokens(token, space_id, created_by)"
        " VALUES('t-old', 'sp-1', 'admin')"
    )
    c.execute(
        "INSERT INTO public_space_cache(space_id, instance_id, name)"
        " VALUES('sp-far', 'host', 'Far')"
    )


def test_existing_rows_keep_null(tmp_path):
    c = _fresh(tmp_path)
    apply_through(c, _VERSION - 1)
    _rows(c)
    apply_through(c, _VERSION)
    assert c.execute("SELECT gfs_relay FROM space_join_requests").fetchone()[0] is None
    tok = c.execute(
        "SELECT bound_instance_id, bound_user_id FROM space_invite_tokens"
    ).fetchone()
    assert tuple(tok) == (None, None)
    assert c.execute("SELECT count(*) FROM public_space_join_pins").fetchone()[0] == 0


def test_columns_round_trip(tmp_path):
    c = _fresh(tmp_path)
    apply_through(c, _VERSION)
    _rows(c)
    c.execute("UPDATE space_join_requests SET gfs_relay='{\"a\": 1}'")
    c.execute(
        "UPDATE space_invite_tokens SET bound_instance_id='hh', bound_user_id='u'"
    )
    c.execute(
        "INSERT INTO public_space_join_pins(space_id, pin) VALUES('sp-far', '{}')"
    )
    # The pin is not the cache row: dropping the listing leaves it.
    c.execute("DELETE FROM public_space_cache")
    assert c.execute("SELECT pin FROM public_space_join_pins").fetchone()[0] == "{}"
    assert c.execute("SELECT gfs_relay FROM space_join_requests").fetchone()[0] == (
        '{"a": 1}'
    )
    assert tuple(
        c.execute(
            "SELECT bound_instance_id, bound_user_id FROM space_invite_tokens"
        ).fetchone()
    ) == ("hh", "u")
