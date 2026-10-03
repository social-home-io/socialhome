"""Migration 0071 — ``space_tasks.deleted_at`` / ``deleted_by`` (single-task
tombstones for §25.6 sync and the resume replay).

Additive: an existing task row survives untouched and reads live (NULL);
the 0069 list-tombstone trigger still drops a list's tasks, tombstones
included.
"""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

_VERSION = 71


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
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp1','S','host','anna','ab')"
    )
    c.execute(
        "INSERT INTO space_task_lists(id, space_id, name, created_by)"
        " VALUES('l1','sp1','L','u1')"
    )
    c.execute(
        "INSERT INTO space_tasks(id, list_id, space_id, title, created_by)"
        " VALUES('t1','l1','sp1','Buy milk','u1')"
    )
    yield c
    c.close()


def test_existing_rows_survive_and_read_live(conn):
    _apply_through(conn, _VERSION)
    row = conn.execute(
        "SELECT title, deleted_at, deleted_by FROM space_tasks WHERE id='t1'"
    ).fetchone()
    assert tuple(row) == ("Buy milk", None, None)


def test_a_list_tombstone_tombstones_its_tasks_in_place(conn):
    """The recreated trigger keeps the rows (so a task id can't be re-filed
    under another list), blanks their content, and stamps the list's
    deleter — but never rewrites a task tombstoned before the list."""
    _apply_through(conn, _VERSION)
    conn.execute(
        "UPDATE space_tasks SET priority='high', labels_json='[\"x\"]',"
        " description='d' WHERE id='t1'"
    )
    conn.execute(
        "INSERT INTO space_tasks(id, list_id, space_id, title, created_by,"
        " deleted_at, deleted_by) VALUES('t0','l1','sp1','','u1',"
        " '2026-01-01 00:00:00','u-early')"
    )
    conn.execute(
        "UPDATE space_task_lists SET deleted_at='2026-02-02 00:00:00',"
        " deleted_by='u-list' WHERE id='l1'"
    )
    rows = {
        r["id"]: tuple(r)[1:]
        for r in conn.execute(
            "SELECT id, deleted_at, deleted_by, title, description, priority,"
            " labels_json FROM space_tasks ORDER BY id"
        )
    }
    assert rows == {
        "t0": ("2026-01-01 00:00:00", "u-early", "", None, None, "[]"),
        "t1": ("2026-02-02 00:00:00", "u-list", "", None, None, "[]"),
    }


def test_the_trigger_is_replaced_not_duplicated(conn):
    _apply_through(conn, _VERSION)
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger'"
        " AND name='space_task_lists_tombstone_drops_tasks'"
    ).fetchall()
    assert len(sql) == 1
    assert "UPDATE space_tasks" in sql[0][0]
    assert "DELETE FROM space_tasks" not in sql[0][0]
