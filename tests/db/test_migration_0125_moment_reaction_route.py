"""Migration 0125 — ``moments.reaction_route`` (v_67): applies over existing
rows untouched, holds valid JSON on relayed rows only, and a tombstone never
keeps one."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 125
_ROUTE = '{"kw":"00","via":"inst-b","rt":["t1"],"rt_suite":"aesgcm-256"}'


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
    for mid, via in (("m-relayed", "household"), ("m-own", "self")):
        c.execute(
            "INSERT INTO moments(id, author_user_id, content, origin_instance_id,"
            " expires_at, received_via) VALUES(?, 'u-a', 'x', 'inst',"
            " datetime('now', '+1 day'), ?)",
            (mid, via),
        )
    before = [tuple(r) for r in c.execute("SELECT * FROM moments ORDER BY id")]
    apply_through(c, _VERSION)
    after = [tuple(r) for r in c.execute("SELECT * FROM moments ORDER BY id")]
    # Every existing column unchanged; the new one NULL.
    assert [r[:-1] for r in after] == before
    assert all(r[-1] is None for r in after)
    yield c
    c.close()


def _route(c, mid: str) -> str | None:
    row = c.execute("SELECT reaction_route FROM moments WHERE id=?", (mid,)).fetchone()
    return row[0]


def test_a_relayed_moment_keeps_a_route(conn):
    conn.execute("UPDATE moments SET reaction_route=? WHERE id='m-relayed'", (_ROUTE,))
    assert _route(conn, "m-relayed") == _ROUTE


def test_only_valid_json_on_a_relayed_row(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE moments SET reaction_route='not json' WHERE id='m-relayed'"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE moments SET reaction_route=? WHERE id='m-own'", (_ROUTE,))


def test_a_tombstone_drops_the_route(conn):
    conn.execute("UPDATE moments SET reaction_route=? WHERE id='m-relayed'", (_ROUTE,))
    conn.execute(
        "UPDATE moments SET content='', deleted_at=datetime('now') WHERE id='m-relayed'"
    )
    assert _route(conn, "m-relayed") is None
