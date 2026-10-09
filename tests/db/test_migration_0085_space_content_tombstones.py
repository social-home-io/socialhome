"""Migration 0085 — space stickies, calendar events, gallery albums / items
and zones keep a tombstone; a deleted space post holds no poll.

Additive columns (every existing row stays live), body-dropping triggers on
the live → tombstoned transitions, and a repair of the poll / schedule rows
already left behind by deleted posts.
"""

from __future__ import annotations

import sqlite3

import pytest

from socialhome.db.migrations import discover_migrations

_VERSION = 85


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
        " identity_public_key) VALUES('sp','S','host','anna','ab')"
    )
    c.execute(
        "INSERT INTO stickies(id, space_id, author, content) VALUES('st','sp','u','x')"
    )
    c.execute(
        "INSERT INTO space_calendar_events(id, space_id, summary, start_dt, end_dt,"
        " created_by) VALUES('ev','sp','Party','2026-01-01T10:00:00',"
        " '2026-01-01T11:00:00','u')"
    )
    c.execute(
        "INSERT INTO space_calendar_rsvps(event_id, user_id, occurrence_at, status)"
        " VALUES('ev','u','2026-01-01T10:00:00','going')"
    )
    c.execute(
        "INSERT INTO space_calendar_rsvp_reminders(event_id, user_id,"
        " occurrence_at, minutes_before, fire_at)"
        " VALUES('ev','u','2026-01-01T10:00:00',10,'2026-01-01T09:50:00')"
    )
    c.execute(
        "INSERT INTO gallery_albums(id, space_id, owner_user_id, name)"
        " VALUES('al','sp','u','Trip')"
    )
    c.execute(
        "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type, filename,"
        " thumbnail_filename, width, height, caption)"
        " VALUES('it','al','u','photo','f.webp','t.webp',10,10,'beach')"
    )
    c.execute(
        "INSERT INTO space_zones(id, space_id, name, latitude, longitude,"
        " radius_m, created_by) VALUES('z','sp','Home',1.0,2.0,100,'u')"
    )
    # A poll and a schedule on a live post, and on one deleted before 0085.
    for pid, deleted in (("p-live", 0), ("p-gone", 1)):
        c.execute(
            "INSERT INTO space_posts(id, space_id, author, type, deleted)"
            " VALUES(?,'sp','u','poll',?)",
            (pid, deleted),
        )
        c.execute("INSERT INTO space_polls(post_id, question) VALUES(?, 'Q?')", (pid,))
        c.execute(
            "INSERT INTO space_poll_options(id, post_id, text) VALUES(?, ?, 'A')",
            (f"o-{pid}", pid),
        )
        c.execute(
            "INSERT INTO space_poll_votes(option_id, voter_user_id) VALUES(?, 'u')",
            (f"o-{pid}",),
        )
        c.execute(
            "INSERT INTO space_schedule_poll_meta(post_id, title) VALUES(?, 'When?')",
            (pid,),
        )
        c.execute(
            "INSERT INTO space_schedule_slots(id, post_id, slot_date)"
            " VALUES(?, ?, '2026-02-01')",
            (f"s-{pid}", pid),
        )
        c.execute(
            "INSERT INTO space_schedule_responses(slot_id, user_id, availability)"
            " VALUES(?, 'u', 'yes')",
            (f"s-{pid}",),
        )
    yield c
    c.close()


def _count(conn, sql: str, *args) -> int:
    return int(conn.execute(sql, args).fetchone()[0])


def _poll_rows(conn, pid: str) -> int:
    return sum(
        _count(conn, sql, pid)
        for sql in (
            "SELECT COUNT(*) FROM space_polls WHERE post_id=?",
            "SELECT COUNT(*) FROM space_poll_options WHERE post_id=?",
            "SELECT COUNT(*) FROM space_poll_votes WHERE option_id IN"
            " (SELECT id FROM space_poll_options WHERE post_id=?)",
            "SELECT COUNT(*) FROM space_schedule_poll_meta WHERE post_id=?",
            "SELECT COUNT(*) FROM space_schedule_slots WHERE post_id=?",
            "SELECT COUNT(*) FROM space_schedule_responses WHERE slot_id IN"
            " (SELECT id FROM space_schedule_slots WHERE post_id=?)",
        )
    )


@pytest.mark.parametrize(
    ("table", "row_id"),
    [
        ("stickies", "st"),
        ("space_calendar_events", "ev"),
        ("gallery_albums", "al"),
        ("gallery_items", "it"),
        ("space_zones", "z"),
    ],
)
def test_every_existing_row_stays_live(conn, table, row_id):
    _apply_through(conn, _VERSION)
    row = conn.execute(
        f"SELECT deleted_at, deleted_by FROM {table} WHERE id=?", (row_id,)
    ).fetchone()
    assert (row["deleted_at"], row["deleted_by"]) == (None, None)


def test_the_poll_rows_of_an_already_deleted_post_are_repaired_away(conn):
    assert _poll_rows(conn, "p-gone") == 6
    _apply_through(conn, _VERSION)
    assert _poll_rows(conn, "p-gone") == 0
    assert _poll_rows(conn, "p-live") == 6


def test_deleting_a_post_drops_its_poll_and_schedule(conn):
    _apply_through(conn, _VERSION)
    conn.execute("UPDATE space_posts SET deleted=1 WHERE id='p-live'")
    assert _poll_rows(conn, "p-live") == 0


def test_a_deleted_post_takes_no_new_poll_or_schedule(conn):
    _apply_through(conn, _VERSION)
    for sql, args in (
        ("INSERT INTO space_polls(post_id, question) VALUES(?, 'Q?')", ("p-gone",)),
        (
            "INSERT INTO space_schedule_poll_meta(post_id, title) VALUES(?, 'W')",
            ("p-gone",),
        ),
    ):
        conn.execute(sql, args)
    assert _poll_rows(conn, "p-gone") == 0
    # A live post still takes one.
    conn.execute(
        "INSERT INTO space_posts(id, space_id, author, type)"
        " VALUES('p-new','sp','u','poll')"
    )
    conn.execute("INSERT INTO space_polls(post_id, question) VALUES('p-new','Q')")
    conn.execute(
        "INSERT INTO space_poll_options(id, post_id, text) VALUES('o-new','p-new','A')"
    )
    assert _poll_rows(conn, "p-new") == 2


def test_a_calendar_tombstone_drops_its_rsvps_and_reminders(conn):
    _apply_through(conn, _VERSION)
    conn.execute(
        "UPDATE space_calendar_events SET deleted_at=datetime('now') WHERE id='ev'"
    )
    assert _count(conn, "SELECT COUNT(*) FROM space_calendar_rsvps") == 0
    assert _count(conn, "SELECT COUNT(*) FROM space_calendar_rsvp_reminders") == 0


def test_an_album_tombstone_tombstones_its_items_in_place(conn):
    _apply_through(conn, _VERSION)
    conn.execute(
        "UPDATE gallery_albums SET deleted_at='2026-03-01 10:00:00',"
        " deleted_by='u-adm' WHERE id='al'"
    )
    row = conn.execute(
        "SELECT deleted_at, deleted_by, filename, thumbnail_filename, caption"
        " FROM gallery_items WHERE id='it'"
    ).fetchone()
    assert tuple(row) == ("2026-03-01 10:00:00", "u-adm", "", "", None)
