"""Migration 0086 — the §25.6 change stamp (``sync_seq``) and the
per-household sync watermark on ``space_instances``.

Triggers stamp every covered row from one monotonic counter on insert and
on any change to a synced column — and on nothing else, so an idempotent
re-apply never stamps. A post's poll / schedule / bazaar rows touch the
post. The tripwire at the bottom fails when a covered table gains a column
its update trigger does not compare: a change to that column would never
reach a household by incremental sync.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

_VERSION = 86

#: Every stamped table and the columns its update trigger may leave out of
#: the comparison (local bookkeeping an idempotent apply may rewrite).
STAMPED: dict[str, frozenset[str]] = {
    "space_posts": frozenset(),
    "space_post_comments": frozenset(),
    "conversation_messages": frozenset({"media_sync_status"}),
    "gallery_albums": frozenset({"updated_at"}),
    "gallery_items": frozenset(),
    "space_calendar_events": frozenset({"updated_at", "notified_at"}),
    "stickies": frozenset({"updated_at"}),
    "space_zones": frozenset({"updated_at"}),
}

#: Tables whose rows are streamed keyed by their post: a change touches it.
TOUCHING: dict[str, frozenset[str]] = {
    "space_polls": frozenset(),
    "space_poll_options": frozenset(),
    "space_schedule_poll_meta": frozenset(),
    "space_schedule_slots": frozenset(),
    "bazaar_listings": frozenset(),
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


@pytest.fixture
def conn(tmp_path):
    c = _fresh(tmp_path, "t.db")
    _apply_through(c, _VERSION)
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp','S','host','anna','ab')"
    )
    c.execute(
        "INSERT INTO space_posts(id, space_id, author, type, content)"
        " VALUES('p','sp','u','text','hello')"
    )
    return c


def _seq(c: sqlite3.Connection, table: str, rid: str) -> int | None:
    return c.execute(f"SELECT sync_seq FROM {table} WHERE id=?", (rid,)).fetchone()[0]


def _counter(c: sqlite3.Connection) -> int:
    return c.execute("SELECT seq FROM sync_seq_counter WHERE id=1").fetchone()[0]


# ── Upgrade ───────────────────────────────────────────────────────────────


def test_existing_rows_stay_unstamped_and_the_counter_starts_at_zero(tmp_path):
    c = _fresh(tmp_path, "up.db")
    _apply_through(c, _VERSION - 1)
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp','S','host','anna','ab')"
    )
    c.execute(
        "INSERT INTO space_posts(id, space_id, author, type, content)"
        " VALUES('old','sp','u','text','x')"
    )
    c.execute("INSERT INTO space_instances(space_id, instance_id) VALUES('sp','peer')")
    _apply_through(c, _VERSION)
    assert _seq(c, "space_posts", "old") is None
    assert _counter(c) == 0
    row = c.execute(
        "SELECT synced_seq, synced_shape, synced_full_at FROM space_instances"
    ).fetchone()
    assert tuple(row) == (None, None, None)
    # Its first change stamps it.
    c.execute("UPDATE space_posts SET content='y' WHERE id='old'")
    assert _seq(c, "space_posts", "old") == 1


# ── Stamping ──────────────────────────────────────────────────────────────


def test_insert_stamps_from_one_monotonic_counter(conn):
    first = _seq(conn, "space_posts", "p")
    conn.execute(
        "INSERT INTO space_post_comments(id, post_id, author, content)"
        " VALUES('k','p','u','hi')"
    )
    assert first is not None
    assert _seq(conn, "space_post_comments", "k") == first + 1 == _counter(conn)


def test_an_edit_reaction_moderation_and_soft_delete_each_restamp(conn):
    stamps = [_seq(conn, "space_posts", "p")]
    for sql in (
        "UPDATE space_posts SET content='edited', edited_at='2026-01-01' WHERE id='p'",
        "UPDATE space_posts SET reactions='{\"x\":[\"u\"]}' WHERE id='p'",
        "UPDATE space_posts SET moderated=1, moderated_by='m' WHERE id='p'",
        "UPDATE space_posts SET deleted=1, content=NULL WHERE id='p'",
    ):
        conn.execute(sql)
        stamps.append(_seq(conn, "space_posts", "p"))
    assert stamps == sorted(set(stamps)), stamps


def test_an_idempotent_reapply_stamps_nothing(conn):
    before = _seq(conn, "space_posts", "p")
    counter = _counter(conn)
    conn.execute("UPDATE space_posts SET content='hello' WHERE id='p'")
    conn.execute(
        "INSERT INTO space_posts(id, space_id, author, type, content)"
        " VALUES('p','sp','u','text','hello')"
        " ON CONFLICT(id) DO UPDATE SET content=excluded.content"
    )
    assert _seq(conn, "space_posts", "p") == before
    assert _counter(conn) == counter


def test_local_bookkeeping_columns_do_not_stamp(conn):
    conn.execute(
        "INSERT INTO space_calendar_events(id, space_id, summary, start_dt, end_dt,"
        " created_by) VALUES('ev','sp','Party','2026-01-01T10:00:00',"
        " '2026-01-01T11:00:00','u')"
    )
    before = _seq(conn, "space_calendar_events", "ev")
    conn.execute(
        "UPDATE space_calendar_events SET notified_at='x', updated_at='y' WHERE id='ev'"
    )
    assert _seq(conn, "space_calendar_events", "ev") == before
    conn.execute("UPDATE space_calendar_events SET summary='Bash' WHERE id='ev'")
    assert _seq(conn, "space_calendar_events", "ev") > before


def test_a_tombstone_stamps_and_the_album_trigger_stamps_its_items(conn):
    conn.execute(
        "INSERT INTO gallery_albums(id, space_id, owner_user_id, name)"
        " VALUES('al','sp','u','Trip')"
    )
    conn.execute(
        "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type, filename,"
        " thumbnail_filename, width, height) VALUES('it','al','u','photo','f','t',1,1)"
    )
    item = _seq(conn, "gallery_items", "it")
    album = _seq(conn, "gallery_albums", "al")
    conn.execute(
        "UPDATE gallery_albums SET deleted_at='2026-01-01', name='' WHERE id='al'"
    )
    assert _seq(conn, "gallery_albums", "al") > album
    assert _seq(conn, "gallery_items", "it") > item


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO space_polls(post_id, question) VALUES('p','Q?')",
        "INSERT INTO space_schedule_poll_meta(post_id, title) VALUES('p','When?')",
        "INSERT INTO bazaar_listings(post_id, space_id, seller_user_id, mode, title,"
        " end_time, currency) VALUES('p','sp','u','fixed','Bike','2027','EUR')",
    ],
)
def test_a_post_child_row_touches_the_post(conn, sql):
    before = _seq(conn, "space_posts", "p")
    conn.execute(sql)
    assert _seq(conn, "space_posts", "p") > before


def test_poll_options_votes_and_slots_touch_the_post(conn):
    conn.execute("INSERT INTO space_polls(post_id, question) VALUES('p','Q?')")
    conn.execute(
        "INSERT INTO space_poll_options(id, post_id, text) VALUES('o','p','A')"
    )
    stamps = [_seq(conn, "space_posts", "p")]
    for sql in (
        "INSERT INTO space_poll_votes(option_id, voter_user_id) VALUES('o','u')",
        "DELETE FROM space_poll_votes WHERE option_id='o'",
        "UPDATE space_poll_options SET text='B' WHERE id='o'",
        "INSERT INTO space_schedule_slots(id, post_id, slot_date) VALUES('s','p','d')",
        "DELETE FROM space_schedule_slots WHERE id='s'",
        "UPDATE space_polls SET closed=1 WHERE post_id='p'",
    ):
        conn.execute(sql)
        stamps.append(_seq(conn, "space_posts", "p"))
    assert stamps == sorted(set(stamps)), stamps
    # An idempotent child re-apply touches nothing.
    conn.execute("UPDATE space_poll_options SET text='B' WHERE id='o'")
    assert _seq(conn, "space_posts", "p") == stamps[-1]


def test_a_post_soft_delete_dropping_its_poll_stays_consistent(conn):
    conn.execute("INSERT INTO space_polls(post_id, question) VALUES('p','Q?')")
    conn.execute(
        "INSERT INTO space_poll_options(id, post_id, text) VALUES('o','p','A')"
    )
    conn.execute(
        "INSERT INTO space_poll_votes(option_id, voter_user_id) VALUES('o','u')"
    )
    before = _seq(conn, "space_posts", "p")
    conn.execute("UPDATE space_posts SET deleted=1 WHERE id='p'")
    assert conn.execute("SELECT COUNT(*) FROM space_polls").fetchone()[0] == 0
    after = _seq(conn, "space_posts", "p")
    assert after is not None and after > before


def test_the_space_instances_upsert_keeps_the_watermark(conn):
    conn.execute("INSERT INTO space_instances(space_id, instance_id) VALUES('sp','h')")
    conn.execute(
        "UPDATE space_instances SET synced_seq=7, synced_shape='s',"
        " synced_full_at='t' WHERE instance_id='h'"
    )
    conn.execute(
        "INSERT INTO space_instances(space_id, instance_id) VALUES('sp','h')"
        " ON CONFLICT(space_id, instance_id) DO UPDATE SET"
        " last_seen_at=datetime('now')"
    )
    row = conn.execute(
        "SELECT synced_seq, synced_shape, synced_full_at FROM space_instances"
    ).fetchone()
    assert tuple(row) == (7, "s", "t")


# ── Tripwire: a column a trigger does not compare never syncs ────────────


def _compared_columns(conn: sqlite3.Connection, trigger: str) -> set[str]:
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)
    ).fetchone()
    assert sql is not None, f"trigger {trigger} is missing"
    return set(re.findall(r"NEW\.(\w+)", sql[0]))


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


@pytest.mark.parametrize("table", sorted(STAMPED))
def test_every_stamped_column_is_compared_by_its_update_trigger(conn, table):
    compared = _compared_columns(conn, f"{table}_sync_seq_update")
    missing = _columns(conn, table) - compared - STAMPED[table] - {"sync_seq"}
    assert not missing, (
        f"{table} has column(s) {sorted(missing)} that {table}_sync_seq_update "
        "does not compare: a change to them would never reach a household by "
        "incremental sync. Recreate the trigger with them (or list them as "
        "local bookkeeping here and in docs/protocol/sync.md)."
    )


@pytest.mark.parametrize("table", sorted(TOUCHING))
def test_every_post_child_column_is_compared_by_its_touch_trigger(conn, table):
    compared = _compared_columns(conn, f"{table}_touch_post_update")
    missing = _columns(conn, table) - compared - TOUCHING[table]
    assert not missing, (
        f"{table} has column(s) {sorted(missing)} its touch trigger does not compare"
    )
