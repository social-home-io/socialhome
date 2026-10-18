"""Migration 0101 — calendar event bounds are tz-aware, enforced by the DB.

A date-only / offset-less ``start_dt`` / ``end_dt`` (an API client's
``"2026-10-20"`` before #994) made the range read compare naive and aware
datetimes and 500 the whole calendar. The migration repairs such rows with
``parse_event_bound``'s rules — date-only start = 00:00 of that day in the
row's tz, date-only end = the next day's 00:00, offset-less = UTC — leaves
offset-carrying values and tombstone blanks untouched, then installs
triggers that refuse an offset-less bound from any writer.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 101
_AWARE = "2026-10-21T17:00:00+02:00"


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
    apply_through(c, _VERSION - 1)
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp','S','host','anna','ab')"
    )
    c.execute(
        "INSERT INTO users(username, user_id, display_name) VALUES('anna','u-a','A')"
    )
    c.execute(
        "INSERT INTO calendars(id, name, color, owner_username)"
        " VALUES('cal','C','#000','anna')"
    )
    yield c
    c.close()


def _space_event(c, eid, start, end, *, tz="UTC", deleted=False):
    c.execute(
        "INSERT INTO space_calendar_events(id, space_id, summary, start_dt,"
        " end_dt, created_by, tz, deleted_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            eid,
            "sp",
            eid,
            start,
            end,
            "u-a",
            tz,
            "2026-10-01 00:00:00" if deleted else None,
        ),
    )


def _personal_event(c, eid, start, end, *, tz="UTC"):
    c.execute(
        "INSERT INTO calendar_events(id, calendar_id, summary, start_dt, end_dt,"
        " created_by, tz) VALUES(?,?,?,?,?,?,?)",
        (eid, "cal", eid, start, end, "u-a", tz),
    )


def _migrate(c):
    apply_through(c, _VERSION)


def _bounds(c, table, eid):
    r = c.execute(f"SELECT start_dt, end_dt FROM {table} WHERE id=?", (eid,)).fetchone()
    return r["start_dt"], r["end_dt"]


def test_date_only_rows_become_the_day_in_the_row_tz(conn):
    _space_event(conn, "zurich", "2026-10-20", "2026-10-20", tz="Europe/Zurich")
    _space_event(conn, "utc", "2026-10-20", "2026-10-21")
    _personal_event(conn, "p-zurich", "2026-10-20", "2026-10-20", tz="Europe/Zurich")
    _migrate(conn)
    assert _bounds(conn, "space_calendar_events", "zurich") == (
        "2026-10-19T22:00:00+00:00",
        "2026-10-20T22:00:00+00:00",
    )
    # A date-only end is inclusive: 20–21 Oct is two days.
    assert _bounds(conn, "space_calendar_events", "utc") == (
        "2026-10-20T00:00:00+00:00",
        "2026-10-22T00:00:00+00:00",
    )
    assert _bounds(conn, "calendar_events", "p-zurich") == (
        "2026-10-19T22:00:00+00:00",
        "2026-10-20T22:00:00+00:00",
    )


def test_unknown_tz_anchors_date_only_on_utc(conn):
    _space_event(conn, "mars", "2026-10-20", "2026-10-20", tz="Mars/Base")
    _migrate(conn)
    assert _bounds(conn, "space_calendar_events", "mars") == (
        "2026-10-20T00:00:00+00:00",
        "2026-10-21T00:00:00+00:00",
    )


def test_offsetless_datetime_is_utc(conn):
    _space_event(
        conn, "naive", "2026-10-21T08:00:00", "2026-10-21T09:00", tz="Europe/Zurich"
    )
    _personal_event(conn, "p-naive", "2026-10-21T08:00:00", "2026-10-21T09:00:00")
    _migrate(conn)
    assert _bounds(conn, "space_calendar_events", "naive") == (
        "2026-10-21T08:00:00+00:00",
        "2026-10-21T09:00:00+00:00",
    )
    assert _bounds(conn, "calendar_events", "p-naive") == (
        "2026-10-21T08:00:00+00:00",
        "2026-10-21T09:00:00+00:00",
    )


def test_offset_rows_tombstones_and_garbage_are_untouched(conn):
    _space_event(conn, "aware", _AWARE, "2026-10-21T18:00:00Z")
    _space_event(conn, "stub", "", "", deleted=True)
    _space_event(conn, "junk", "not a date", "2026-10-21T18:00:00+00:00")
    _personal_event(conn, "p-aware", _AWARE, _AWARE)
    _migrate(conn)
    assert _bounds(conn, "space_calendar_events", "aware") == (
        _AWARE,
        "2026-10-21T18:00:00Z",
    )
    assert _bounds(conn, "space_calendar_events", "stub") == ("", "")
    assert _bounds(conn, "space_calendar_events", "junk")[0] == "not a date"
    assert _bounds(conn, "calendar_events", "p-aware") == (_AWARE, _AWARE)


@pytest.mark.parametrize(
    "bad",
    ["2026-10-20", "2026-10-20T10:00:00", "2026-10-20T10:00", "", "tomorrow"],
)
def test_triggers_refuse_an_offsetless_insert(conn, bad):
    _migrate(conn)
    with pytest.raises(sqlite3.IntegrityError, match="tz-aware"):
        _space_event(conn, "new", bad, _AWARE)
    with pytest.raises(sqlite3.IntegrityError, match="tz-aware"):
        _personal_event(conn, "p-new", _AWARE, bad)


def test_triggers_refuse_an_offsetless_update(conn):
    _migrate(conn)
    _space_event(conn, "ok", _AWARE, _AWARE)
    _personal_event(conn, "p-ok", _AWARE, _AWARE)
    with pytest.raises(sqlite3.IntegrityError, match="tz-aware"):
        conn.execute(
            "UPDATE space_calendar_events SET end_dt='2026-10-22' WHERE id='ok'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="tz-aware"):
        conn.execute(
            "UPDATE calendar_events SET start_dt='2026-10-22T10:00' WHERE id='p-ok'"
        )
    # Other columns stay writable on any row.
    conn.execute("UPDATE space_calendar_events SET summary='x' WHERE id='ok'")


def test_triggers_accept_aware_values_and_tombstone_stubs(conn):
    _migrate(conn)
    _space_event(conn, "z", "2026-10-21T08:00:00Z", "2026-10-21T09:00:00.5-05:00")
    _personal_event(conn, "p", _AWARE, "2026-10-21 18:00:00+00:00")
    # The tombstone insert stub (0085) and the delete-in-place UPDATE.
    _space_event(conn, "stub", "", "", deleted=True)
    conn.execute(
        "UPDATE space_calendar_events SET deleted_at=datetime('now'),"
        " start_dt='', end_dt='' WHERE id='z'"
    )
    assert _bounds(conn, "space_calendar_events", "z") == ("", "")
    # A blank bound on a LIVE row is refused.
    with pytest.raises(sqlite3.IntegrityError, match="tz-aware"):
        _space_event(conn, "live-blank", "", "")
