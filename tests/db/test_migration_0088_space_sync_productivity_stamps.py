"""Migration 0088 — the §25.6 change stamp (``sync_seq``) on space task
lists, tasks, pages and timetables, and the page-snapshot touch triggers.

Each covered table stamps on insert and on a change to a synced column —
an edit, an archive / unarchive, a move to another list, a tombstone, the
list-tombstone cascade — and never on an idempotent re-apply or a
bookkeeping-only write. A page's draft base / conflict side
(``space_page_snapshots``) touches the page.

The tripwire at the bottom runs against the HEAD schema and covers every
stamped table of 0086 and 0088: it fails when a table gains a column its
update trigger does not compare (a change to it would never reach a
household by incremental sync).
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

_VERSION = 88

#: Every stamped table (0086 + 0088) and the columns its update trigger may
#: leave out of the comparison (local bookkeeping).
STAMPED: dict[str, frozenset[str]] = {
    # 0086
    "space_posts": frozenset(),
    "space_post_comments": frozenset(),
    "conversation_messages": frozenset({"media_sync_status"}),
    "gallery_albums": frozenset({"updated_at"}),
    "gallery_items": frozenset(),
    "space_calendar_events": frozenset({"updated_at", "notified_at"}),
    "stickies": frozenset({"updated_at"}),
    "space_zones": frozenset({"updated_at"}),
    # 0088
    "space_task_lists": frozenset({"updated_at"}),
    "space_tasks": frozenset({"updated_at"}),
    "space_pages": frozenset({"updated_at"}),
    "space_timetables": frozenset({"updated_at"}),
}

#: Tables whose rows are part of another table's streamed record: a change
#: touches the parent (``<table>_touch_<parent>_update``).
TOUCHING: dict[str, tuple[str, frozenset[str]]] = {
    "space_polls": ("post", frozenset()),
    "space_poll_options": ("post", frozenset()),
    "space_schedule_poll_meta": ("post", frozenset()),
    "space_schedule_slots": ("post", frozenset()),
    "bazaar_listings": ("post", frozenset()),
    "space_page_snapshots": ("page", frozenset()),
}


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


def _fresh(tmp_path, name: str) -> sqlite3.Connection:
    c = sqlite3.connect(tmp_path / name, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    return c


def _space(c: sqlite3.Connection) -> None:
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp','S','host','anna','ab')"
    )


@pytest.fixture
def conn(tmp_path):
    c = _fresh(tmp_path, "t.db")
    _apply_through(c, _VERSION)
    _space(c)
    c.execute(
        "INSERT INTO space_task_lists(id, space_id, name, created_by)"
        " VALUES('l1','sp','Chores','u'), ('l2','sp','Shop','u')"
    )
    c.execute(
        "INSERT INTO space_tasks(id, list_id, space_id, title, created_by)"
        " VALUES('t1','l1','sp','Dishes','u'), ('t2','l1','sp','Bins','u')"
    )
    c.execute(
        "INSERT INTO space_pages(id, space_id, title, content, created_by)"
        " VALUES('pg','sp','Wiki','body','u')"
    )
    c.execute(
        "INSERT INTO space_timetables(id, space_id, name, created_by)"
        " VALUES('tt','sp','School','u')"
    )
    return c


def _seq(c: sqlite3.Connection, table: str, rid: str) -> int | None:
    return c.execute(f"SELECT sync_seq FROM {table} WHERE id=?", (rid,)).fetchone()[0]


def _counter(c: sqlite3.Connection) -> int:
    return c.execute("SELECT seq FROM sync_seq_counter WHERE id=1").fetchone()[0]


# ── Upgrade ───────────────────────────────────────────────────────────────


def test_existing_rows_stay_unstamped_until_they_change(tmp_path):
    c = _fresh(tmp_path, "up.db")
    _apply_through(c, _VERSION - 1)
    _space(c)
    c.execute(
        "INSERT INTO space_task_lists(id, space_id, name, created_by)"
        " VALUES('old','sp','L','u')"
    )
    before = _counter(c)
    _apply_through(c, _VERSION)
    assert _seq(c, "space_task_lists", "old") is None
    assert _counter(c) == before
    c.execute("UPDATE space_task_lists SET name='M' WHERE id='old'")
    assert _seq(c, "space_task_lists", "old") == before + 1


# ── Stamping ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("table", "rid"),
    [
        ("space_task_lists", "l1"),
        ("space_tasks", "t1"),
        ("space_pages", "pg"),
        ("space_timetables", "tt"),
    ],
)
def test_insert_stamps(conn, table, rid):
    assert _seq(conn, table, rid) is not None


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE space_tasks SET title='Edited' WHERE id='t1'",
        "UPDATE space_tasks SET status='done' WHERE id='t1'",
        "UPDATE space_tasks SET archived_at='2026-10-09' WHERE id='t1'",
        "UPDATE space_tasks SET list_id='l2' WHERE id='t1'",
        "UPDATE space_tasks SET position=5 WHERE id='t1'",
        "UPDATE space_tasks SET deleted_at='2026', title='' WHERE id='t1'",
    ],
)
def test_a_task_edit_archive_move_and_delete_each_stamp(conn, sql):
    before = _seq(conn, "space_tasks", "t1")
    conn.execute(sql)
    assert _seq(conn, "space_tasks", "t1") > before


def test_unarchive_stamps_again(conn):
    conn.execute("UPDATE space_tasks SET archived_at='2026-10-09' WHERE id='t1'")
    archived = _seq(conn, "space_tasks", "t1")
    conn.execute("UPDATE space_tasks SET archived_at=NULL WHERE id='t1'")
    assert _seq(conn, "space_tasks", "t1") > archived


def test_a_list_tombstone_stamps_the_list_and_cascades_to_its_tasks(conn):
    lst = _seq(conn, "space_task_lists", "l1")
    tasks = [_seq(conn, "space_tasks", t) for t in ("t1", "t2")]
    conn.execute("UPDATE space_task_lists SET deleted_at=datetime('now') WHERE id='l1'")
    assert _seq(conn, "space_task_lists", "l1") > lst
    for tid, before in zip(("t1", "t2"), tasks):
        assert _seq(conn, "space_tasks", tid) > before


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE space_pages SET content='new body', seq=1 WHERE id='pg'",
        "UPDATE space_pages SET title='Renamed' WHERE id='pg'",
        "UPDATE space_pages SET deleted_at=datetime('now'), title='', content=''"
        " WHERE id='pg'",
    ],
)
def test_a_page_edit_and_tombstone_stamp(conn, sql):
    before = _seq(conn, "space_pages", "pg")
    conn.execute(sql)
    after = _seq(conn, "space_pages", "pg")
    assert after is not None and after > before


def test_a_timetable_edit_and_delete_stamp(conn):
    before = _seq(conn, "space_timetables", "tt")
    conn.execute("UPDATE space_timetables SET name='Work', version=2 WHERE id='tt'")
    edited = _seq(conn, "space_timetables", "tt")
    assert edited > before
    conn.execute("UPDATE space_timetables SET deleted_at='2026' WHERE id='tt'")
    assert _seq(conn, "space_timetables", "tt") > edited


def test_an_idempotent_reapply_and_bookkeeping_stamp_nothing(conn):
    stamps = {
        t: _seq(conn, t, r)
        for t, r in (
            ("space_task_lists", "l1"),
            ("space_tasks", "t1"),
            ("space_pages", "pg"),
            ("space_timetables", "tt"),
        )
    }
    counter = _counter(conn)
    conn.execute(
        "INSERT INTO space_task_lists(id, space_id, name, created_by)"
        " VALUES('l1','sp','Chores','u') ON CONFLICT(id) DO UPDATE SET"
        " name=excluded.name, updated_at=datetime('now')"
    )
    conn.execute("UPDATE space_tasks SET title='Dishes', updated_at='x' WHERE id='t1'")
    conn.execute("UPDATE space_pages SET updated_at='x' WHERE id='pg'")
    conn.execute("UPDATE space_timetables SET updated_at='x' WHERE id='tt'")
    assert _counter(conn) == counter
    for (t, r), before in zip(
        (
            ("space_task_lists", "l1"),
            ("space_tasks", "t1"),
            ("space_pages", "pg"),
            ("space_timetables", "tt"),
        ),
        stamps.values(),
    ):
        assert _seq(conn, t, r) == before


def test_a_space_page_snapshot_touches_its_page(conn):
    stamps = [_seq(conn, "space_pages", "pg")]
    for sql in (
        "INSERT INTO space_page_snapshots(page_id, space_id, snapshot_at, body,"
        " snapshot_by, side, conflict) VALUES('pg','sp','t1','b','u','theirs',1)",
        "UPDATE space_page_snapshots SET conflict=0 WHERE page_id='pg'",
        "DELETE FROM space_page_snapshots WHERE page_id='pg'",
        "INSERT INTO space_page_snapshots(page_id, space_id, snapshot_at, body,"
        " snapshot_by, side) VALUES('pg','sp','t2','b','u','base')",
    ):
        conn.execute(sql)
        stamps.append(_seq(conn, "space_pages", "pg"))
    assert stamps == sorted(set(stamps)), stamps
    # An idempotent snapshot update touches nothing.
    conn.execute("UPDATE space_page_snapshots SET body='b' WHERE page_id='pg'")
    assert _seq(conn, "space_pages", "pg") == stamps[-1]


def test_a_household_snapshot_touches_no_space_page(conn):
    before = _seq(conn, "space_pages", "pg")
    counter = _counter(conn)
    conn.execute(
        "INSERT INTO space_page_snapshots(page_id, space_id, snapshot_at, body,"
        " snapshot_by) VALUES('pg', NULL, 't9', 'b', 'u')"
    )
    conn.execute("DELETE FROM space_page_snapshots WHERE space_id IS NULL")
    assert _seq(conn, "space_pages", "pg") == before
    assert _counter(conn) == counter


def test_a_page_tombstone_dropping_its_snapshots_leaves_a_stamp(conn):
    """The 0073 trigger deletes the page's snapshots inside the tombstone
    update; the touch must not leave the page unstamped (NULL)."""
    conn.execute(
        "INSERT INTO space_page_snapshots(page_id, space_id, snapshot_at, body,"
        " snapshot_by, side, conflict) VALUES('pg','sp','t1','b','u','theirs',1)"
    )
    before = _seq(conn, "space_pages", "pg")
    conn.execute("UPDATE space_pages SET deleted_at=datetime('now') WHERE id='pg'")
    after = _seq(conn, "space_pages", "pg")
    assert after is not None and after > before


# ── Tripwire (HEAD schema): a column a trigger does not compare never syncs ─


@pytest.fixture
def head(tmp_path):
    c = _fresh(tmp_path, "head.db")
    _apply_through(c, 10**6)
    return c


def _compared_columns(conn: sqlite3.Connection, trigger: str) -> set[str]:
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)
    ).fetchone()
    assert sql is not None, f"trigger {trigger} is missing"
    return set(re.findall(r"NEW\.(\w+)", sql[0]))


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


@pytest.mark.parametrize("table", sorted(STAMPED))
def test_every_stamped_column_is_compared_by_its_update_trigger(head, table):
    compared = _compared_columns(head, f"{table}_sync_seq_update")
    missing = _columns(head, table) - compared - STAMPED[table] - {"sync_seq"}
    assert not missing, (
        f"{table} has column(s) {sorted(missing)} that {table}_sync_seq_update "
        "does not compare: a change to them would never reach a household by "
        "incremental sync. Recreate the trigger with them (or list them as "
        "local bookkeeping here and in docs/protocol/sync.md)."
    )


@pytest.mark.parametrize("table", sorted(TOUCHING))
def test_every_child_column_is_compared_by_its_touch_trigger(head, table):
    parent, allowed = TOUCHING[table]
    compared = _compared_columns(head, f"{table}_touch_{parent}_update")
    missing = _columns(head, table) - compared - allowed
    assert not missing, (
        f"{table} has column(s) {sorted(missing)} its touch trigger does not compare"
    )
