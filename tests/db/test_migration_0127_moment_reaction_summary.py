"""Migration 0127 — ``moments.reaction_summary`` (v_69): applies over existing
rows untouched, holds valid JSON on moments from other households only, and a
tombstone never keeps one."""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 127
_SUMMARY = '{"rev":1761480000000,"counts":{"🔥":2},"total":3}'


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
    for mid, via in (("m-relayed", "household"), ("m-gfs", "gfs"), ("m-own", "self")):
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


def _summary(c, mid: str) -> str | None:
    row = c.execute(
        "SELECT reaction_summary FROM moments WHERE id=?", (mid,)
    ).fetchone()
    return row[0]


@pytest.mark.parametrize("mid", ["m-relayed", "m-gfs"])
def test_a_moment_from_another_household_keeps_totals(conn, mid):
    conn.execute("UPDATE moments SET reaction_summary=? WHERE id=?", (_SUMMARY, mid))
    assert _summary(conn, mid) == _SUMMARY


def test_only_valid_json_and_never_on_an_own_moment(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE moments SET reaction_summary='nope' WHERE id='m-relayed'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE moments SET reaction_summary=? WHERE id='m-own'", (_SUMMARY,)
        )


def test_a_tombstone_drops_the_totals(conn):
    conn.execute(
        "UPDATE moments SET reaction_summary=? WHERE id='m-relayed'", (_SUMMARY,)
    )
    conn.execute(
        "UPDATE moments SET content='', deleted_at=datetime('now') WHERE id='m-relayed'"
    )
    assert _summary(conn, "m-relayed") is None


def test_a_re_save_keeps_the_totals(conn):
    """The repo's upsert names its columns — a closer relay's re-save of the
    content row leaves the totals alone."""
    conn.execute(
        "UPDATE moments SET reaction_summary=? WHERE id='m-relayed'", (_SUMMARY,)
    )
    conn.execute(
        "INSERT INTO moments(id, author_user_id, content, origin_instance_id,"
        " expires_at, received_via, hop_count) VALUES('m-relayed', 'u-a', 'y',"
        " 'inst', datetime('now', '+1 day'), 'household', 1)"
        " ON CONFLICT(id) DO UPDATE SET content=excluded.content,"
        " hop_count=excluded.hop_count"
    )
    assert _summary(conn, "m-relayed") == _SUMMARY
