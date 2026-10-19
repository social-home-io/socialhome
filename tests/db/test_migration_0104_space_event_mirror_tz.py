"""Migration 0104 — a space event's RSVP mirror keeps the event's tz.

Repairs the mirrors on disk (tz + bounds copied from the live source) and
refuses a mirror whose tz differs from its live source from then on.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.migration_chain import apply_through

_VERSION = 104

_SRC = (
    "INSERT INTO space_calendar_events(id, space_id, summary, start_dt, end_dt,"
    " all_day, created_by, tz, deleted_at) VALUES(?, 'sp', 'Holidays', ?, ?, 1,"
    " 'u1', ?, ?)"
)
_MIRROR = (
    "INSERT INTO calendar_events(id, calendar_id, summary, start_dt, end_dt,"
    " all_day, created_by, mirrored_from, tz) VALUES(?, 'cal', 'Holidays', ?, ?,"
    " 1, 'u1', ?, ?)"
)


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    apply_through(c, _VERSION - 1)
    c.execute(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, space_type, join_mode)"
        " VALUES('sp', 'Family', 'inst', 'anna', 'ab', 'private', 'invite_only')"
    )
    c.execute(
        "INSERT INTO calendars(id, name, owner_username) VALUES('cal', 'Anna', 'anna')"
    )
    # A Zurich all-day week, and its mirror as older code stored it: tz
    # UTC and stale bounds (a day off).
    c.execute(
        _SRC,
        (
            "ev",
            "2026-10-25T23:00:00+00:00",
            "2026-10-31T22:59:00+00:00",
            "Europe/Zurich",
            None,
        ),
    )
    c.execute(
        _MIRROR,
        ("sm_1", "2026-10-26T00:00:00+00:00", "2026-10-31T00:00:00+00:00", "ev", "UTC"),
    )
    # A tombstoned source: its mirror is left alone.
    c.execute(_SRC, ("gone", "", "", "Europe/Zurich", "2026-10-01 00:00:00"))
    c.execute(
        _MIRROR,
        (
            "sm_2",
            "2026-10-26T00:00:00+00:00",
            "2026-10-27T00:00:00+00:00",
            "gone",
            "UTC",
        ),
    )
    yield c
    c.close()


def _row(conn, rid):
    return conn.execute(
        "SELECT tz, start_dt, end_dt, all_day FROM calendar_events WHERE id=?", (rid,)
    ).fetchone()


def test_repairs_a_mirror_to_its_live_source(conn):
    apply_through(conn, _VERSION)
    row = _row(conn, "sm_1")
    assert tuple(row) == (
        "Europe/Zurich",
        "2026-10-25T23:00:00+00:00",
        "2026-10-31T22:59:00+00:00",
        1,
    )


def test_leaves_a_mirror_of_a_tombstoned_source_alone(conn):
    apply_through(conn, _VERSION)
    assert _row(conn, "sm_2")["tz"] == "UTC"


def test_refuses_a_mirror_with_another_tz(conn):
    apply_through(conn, _VERSION)
    with pytest.raises(sqlite3.IntegrityError, match="keep the event's tz"):
        conn.execute("UPDATE calendar_events SET tz='UTC' WHERE id='sm_1'")
    with pytest.raises(sqlite3.IntegrityError, match="keep the event's tz"):
        conn.execute(
            _MIRROR,
            (
                "sm_3",
                "2026-10-25T23:00:00+00:00",
                "2026-10-31T22:59:00+00:00",
                "ev",
                "UTC",
            ),
        )
    # The matching zone, and a plain household event, are fine.
    conn.execute(
        _MIRROR,
        (
            "sm_3",
            "2026-10-25T23:00:00+00:00",
            "2026-10-31T22:59:00+00:00",
            "ev",
            "Europe/Zurich",
        ),
    )
    conn.execute(
        "INSERT INTO calendar_events(id, calendar_id, summary, start_dt, end_dt,"
        " all_day, created_by, tz) VALUES('own', 'cal', 'Mine',"
        " '2026-10-25T23:00:00+00:00', '2026-10-26T22:59:00+00:00', 1, 'u1', 'UTC')"
    )
