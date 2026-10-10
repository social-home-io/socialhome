"""Migration 0090 — the virtual-occurrence rows an older §25.6 sync stored
as one-off space calendar events (``<series id>@<start>``) are removed with
everything hanging off them, and no such row can be stored again."""

from __future__ import annotations

import sqlite3

from socialhome.db.migrations import discover_migrations

_VERSION = 90

SERIES = "5e71e5"
OCC_START = "2026-10-14T09:20:24.929393+00:00"
STRAY = f"{SERIES}@{OCC_START}"
#: A stray whose series row this household never held (a joiner whose
#: series started outside the old window): the shape alone identifies it.
ORPHAN_START = "2026-10-21T09:20:24+00:00"
ORPHAN = f"gone@{ORPHAN_START}"
#: An id with an ``@`` that is NOT an occurrence: the suffix is not its own
#: start (an ICS-style ``uid@host`` a legacy peer might have minted).
ICS_LIKE = "meeting-42@calendar.example.org"


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


def _fresh(tmp_path) -> sqlite3.Connection:
    c = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, description TEXT,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    return c


def _event(c: sqlite3.Connection, eid: str, start: str, rrule: str | None = None):
    return c.execute(
        "INSERT INTO space_calendar_events(id, space_id, summary, start_dt,"
        " end_dt, rrule, created_by) VALUES(?, 'sp', 'choir', ?, ?, ?, 'u')",
        (eid, start, start, rrule),
    )


def _hanging(c: sqlite3.Connection, eid: str, start: str) -> None:
    c.execute(
        "INSERT INTO space_calendar_rsvps(event_id, user_id, occurrence_at,"
        " status) VALUES(?, 'u-bob', ?, 'going')",
        (eid, start),
    )
    c.execute(
        "INSERT INTO space_calendar_rsvp_reminders(event_id, user_id,"
        " occurrence_at, minutes_before, fire_at) VALUES(?, 'u-bob', ?, 10, ?)",
        (eid, start, start),
    )
    c.execute(
        "INSERT INTO pending_federated_rsvps(event_id, user_id, occurrence_at,"
        " status, updated_at) VALUES(?, 'u-cat', ?, 'going', ?)",
        (eid, start, start),
    )


def _ids(c: sqlite3.Connection, table: str, col: str = "event_id") -> set[str]:
    return {r[0] for r in c.execute(f"SELECT {col} FROM {table}")}


def test_stray_occurrence_rows_go_with_what_hangs_off_them(tmp_path):
    c = _fresh(tmp_path)
    _apply_through(c, _VERSION - 1)
    _event(c, SERIES, "2026-10-07T09:20:24.929393+00:00")  # rule wiped (old bug)
    _event(c, STRAY, OCC_START, "FREQ=WEEKLY")
    _event(c, ORPHAN, ORPHAN_START)
    _event(c, ICS_LIKE, "2026-11-01T10:00:00+00:00")
    for eid, start in ((SERIES, "s"), (STRAY, OCC_START), (ICS_LIKE, "x")):
        _hanging(c, eid, start)
    c.execute(
        "INSERT INTO users(username, user_id, display_name)"
        " VALUES('bob', 'u-bob', 'Bob')"
    )
    c.execute(
        "INSERT INTO calendars(id, name, color, owner_username)"
        " VALUES('cal-bob', 'Bob', '#000', 'bob')"
    )
    for mid, source in (("mirror-stray", STRAY), ("mirror-ok", SERIES)):
        c.execute(
            "INSERT INTO calendar_events(id, calendar_id, summary, start_dt,"
            " end_dt, created_by, mirrored_from) VALUES(?, 'cal-bob', 'choir', 'a', 'b',"
            " 'u-bob', ?)",
            (mid, source),
        )

    _apply_through(c, _VERSION)

    assert _ids(c, "space_calendar_events", "id") == {SERIES, ICS_LIKE}
    for table in (
        "space_calendar_rsvps",
        "space_calendar_rsvp_reminders",
        "pending_federated_rsvps",
    ):
        assert _ids(c, table) == {SERIES, ICS_LIKE}, table
    assert _ids(c, "calendar_events", "id") == {"mirror-ok"}


def test_the_stray_shape_cannot_be_stored_again(tmp_path):
    c = _fresh(tmp_path)
    _apply_through(c, _VERSION)
    _event(c, STRAY, OCC_START)  # ignored by the guard, not an error
    _event(c, ICS_LIKE, "2026-11-01T10:00:00+00:00")
    _event(c, SERIES, "2026-10-07T09:20:24+00:00", "FREQ=WEEKLY")
    # A tombstone stub carries no start: never the stray shape.
    c.execute(
        "INSERT INTO space_calendar_events(id, space_id, summary, start_dt,"
        " end_dt, created_by, deleted_at) VALUES('del@', 'sp', '', '', '',"
        " 'u', datetime('now'))"
    )
    assert _ids(c, "space_calendar_events", "id") == {ICS_LIKE, SERIES, "del@"}
