"""Migration 0067 — ``content_reports`` gains a ``space_id`` scope and the
space content kinds as report targets.

A rebuild (SQLite cannot edit a CHECK in place). These tests run it against
the REAL pre-0067 schema with rows in the table and pin that every value
survives as a household-level report, that the indexes come back, that the
new target kinds are admitted, junk still is not, and that "one report per
(reporter, target)" now holds per scope.
"""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

_VERSION = 67


def _apply_through(conn: sqlite3.Connection, last: int) -> None:
    done = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    for mig in discover_migrations():
        if mig.version in done:
            continue
        if mig.version > last:
            break
        mig.apply(conn)
        conn.execute(
            "INSERT INTO schema_version(version, description) VALUES (?,?)",
            (mig.version, mig.description),
        )


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    _apply_through(c, _VERSION - 1)
    yield c
    c.close()


def _insert(c, rid, *, target_type="post", target_id="p1", reporter="u1", **kw):
    cols = {
        "id": rid,
        "target_type": target_type,
        "target_id": target_id,
        "reporter_user_id": reporter,
        "category": "spam",
        **kw,
    }
    c.execute(
        f"INSERT INTO content_reports({', '.join(cols)}) "
        f"VALUES({', '.join('?' for _ in cols)})",
        tuple(cols.values()),
    )


def test_rows_survive_as_household_reports(conn):
    _insert(
        conn,
        "r1",
        reporter_instance_id="peer",
        notes="n",
        status="resolved",
        created_at="2026-01-02T03:04:05+00:00",
        resolved_by="admin",
        resolved_at="2026-01-03 00:00:00",
    )
    _insert(conn, "r2", target_type="user", target_id="u9")
    _apply_through(conn, _VERSION)
    rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM content_reports")}
    assert rows["r1"] == {
        "id": "r1",
        "target_type": "post",
        "target_id": "p1",
        "reporter_user_id": "u1",
        "reporter_instance_id": "peer",
        "category": "spam",
        "notes": "n",
        "status": "resolved",
        "created_at": "2026-01-02T03:04:05+00:00",
        "resolved_by": "admin",
        "resolved_at": "2026-01-03 00:00:00",
        "space_id": None,
        "sole_reviewer_user_id": None,
    }
    assert rows["r2"]["space_id"] is None
    assert rows["r2"]["status"] == "pending"


def test_indexes_recreated(conn):
    _apply_through(conn, _VERSION)
    names = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='content_reports'"
        )
    }
    assert {
        "idx_content_reports_status",
        "idx_content_reports_reporter",
        "idx_content_reports_unique_pair",
        "idx_content_reports_space",
    } <= names


@pytest.mark.parametrize(
    "kind", ["page", "task", "sticky", "calendar_event", "gallery_item"]
)
def test_new_target_kinds_admitted(conn, kind):
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, "x", target_type=kind)
    _apply_through(conn, _VERSION)
    _insert(conn, "x", target_type=kind, space_id="sp1")


def test_junk_kind_still_refused(conn):
    _apply_through(conn, _VERSION)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, "x", target_type="nonsense")


def test_one_report_per_target_per_scope(conn):
    _apply_through(conn, _VERSION)
    _insert(conn, "a", target_type="user", target_id="bob")
    # Household scope stays unique despite the NULL space_id.
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, "b", target_type="user", target_id="bob")
    # The same member may be reported once in each space.
    _insert(conn, "c", target_type="user", target_id="bob", space_id="sp1")
    _insert(conn, "d", target_type="user", target_id="bob", space_id="sp2")
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, "e", target_type="user", target_id="bob", space_id="sp1")
