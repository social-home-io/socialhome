"""Migration 0073 — ``space_pages.deleted_at`` / ``deleted_by`` (space page
tombstones for §25.6 sync and the resume replay).

Additive: an existing page row survives untouched and reads live (NULL).
The tombstone trigger drops the page's conflict sides, draft base and edit
history in its own space — never another scope's rows sharing the id — and
fires only on the live → tombstoned transition.
"""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

_VERSION = 73


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
        "INSERT INTO space_pages(id, space_id, title, content, created_by, seq)"
        " VALUES('pg1','sp1','Rules','body','u1', 4)"
    )
    yield c
    c.close()


def _bodies(conn: sqlite3.Connection) -> None:
    """History + snapshots of pg1 in sp1, and of a household page pg1."""
    for space_id, version in (("sp1", 1), (None, 2)):
        conn.execute(
            "INSERT INTO page_edit_history(id, page_id, space_id, title, content,"
            " edited_by, version) VALUES(?, 'pg1', ?, 'old', 'old body', 'u1', ?)",
            (f"h{version}", space_id, version),
        )
    for space_id, at, side, conflict in (
        ("sp1", "s1", "theirs", 1),
        ("sp1", "base:pg1", "base", 0),
        (None, "s2", "mine", 0),
    ):
        conn.execute(
            "INSERT INTO space_page_snapshots(page_id, space_id, snapshot_at,"
            " body, snapshot_by, side, conflict) VALUES('pg1', ?, ?, 'x', 'u1', ?, ?)",
            (space_id, at, side, conflict),
        )


def test_existing_rows_survive_and_read_live(conn):
    _apply_through(conn, _VERSION)
    row = conn.execute(
        "SELECT title, content, seq, deleted_at, deleted_by FROM space_pages"
    ).fetchone()
    assert tuple(row) == ("Rules", "body", 4, None, None)


def test_a_tombstone_drops_the_pages_bodies_in_its_own_space(conn):
    _apply_through(conn, _VERSION)
    _bodies(conn)
    conn.execute(
        "UPDATE space_pages SET deleted_at=datetime('now'), deleted_by='u2'"
        " WHERE id='pg1'"
    )
    history = conn.execute(
        "SELECT space_id FROM page_edit_history WHERE page_id='pg1'"
    ).fetchall()
    snaps = conn.execute(
        "SELECT space_id FROM space_page_snapshots WHERE page_id='pg1'"
    ).fetchall()
    # Only the household page's rows (space_id NULL) are left.
    assert [r[0] for r in history] == [None]
    assert [r[0] for r in snaps] == [None]


def test_the_trigger_fires_only_on_the_tombstone_transition(conn):
    _apply_through(conn, _VERSION)
    _bodies(conn)
    conn.execute("UPDATE space_pages SET content='edited' WHERE id='pg1'")
    n_hist = conn.execute("SELECT COUNT(*) FROM page_edit_history").fetchone()[0]
    n_snap = conn.execute("SELECT COUNT(*) FROM space_page_snapshots").fetchone()[0]
    assert (n_hist, n_snap) == (2, 3)
