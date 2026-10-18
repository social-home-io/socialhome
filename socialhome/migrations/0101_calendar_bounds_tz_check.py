"""0101 — calendar event bounds are tz-aware ISO 8601, enforced by the DB.

Follows 0099 (member picture hash repair); 0100 is reserved for the household
auto-membership change, which must land on main BEFORE this one (the runner
applies only versions above the highest recorded, so a later 0100 would be
skipped on every database that already ran 0101).

Before #994 the calendar API stored ``start`` / ``end`` exactly as
``datetime.fromisoformat`` parsed them, so an API client's date-only
``"2026-10-20"`` (or an offset-less ``"2026-10-20T10:00"``) landed in
``start_dt`` / ``end_dt`` next to the ``+00:00`` values every other writer
produces. Reading a calendar compares the two kinds and raises
``TypeError`` — one such row made ``GET …/calendar/events`` answer 500 for
the whole space. #994 normalises every bound on write; this migration
(a) repairs the rows already on disk and (b) installs BEFORE INSERT /
BEFORE UPDATE triggers that refuse an offset-less bound from any writer
(today's code, a regression, a restored backup). The repair runs FIRST,
so no install boot-loops on its own rows.

Repair rules — the same as ``utils.timezones.parse_event_bound``, the
write-side normaliser:

* date-only start → 00:00 of that day in the row's ``tz`` (UTC when the
  zone is unknown), as a UTC instant;
* date-only end → 00:00 of the NEXT day in ``tz`` (the day is included);
* offset-less datetime → the same wall clock read as UTC;
* a value that already carries an offset (``+02:00``, ``Z``) is left
  exactly as stored — recurring-event occurrence ids (and the RSVPs keyed
  on them) are minted from it;
* ``''`` is left alone: it is the content-free tombstone stub of a deleted
  space event (0085), the only place an empty bound is legitimate;
* an unparseable value is left alone and logged at WARNING (none is known
  to exist — every writer parsed it first); it can't be reinterpreted
  safely, and the read path already treats it as "no date".

The date-only rule needs the row's IANA zone, which SQL can't apply —
hence a Python migration (the runner supports ``migrate(conn)``). It is
self-contained (stdlib ``zoneinfo`` only) so later edits to the app code
can't change what this migration did.

Guard: one BEFORE INSERT and one BEFORE UPDATE OF start_dt, end_dt trigger
per table, RAISE(ABORT): a bound must look like
``YYYY-MM-DDTHH:MM…`` and end in ``±HH:MM`` or ``Z``; the space table
also accepts ``''`` on a tombstone row (``deleted_at`` set). A future
migration that rebuilds one of these tables must recreate its triggers.

CLAUDE.md "audit before a migration":

  (1) Audited every writer of ``calendar_events`` / ``space_calendar_events``
      bounds. All SQL lives in ``repositories/calendar_repo.py``
      (``save_event`` upserts, the space ``delete_event`` /
      ``tombstone_event`` stubs with ``''``); every ``CalendarEvent`` reaches
      it through ``_iso``, which stamps a naive datetime UTC. Producers: the
      calendar service create / update (``parse_event_bound``), the live
      ``SPACE_CALENDAR_EVENT_*`` and personal-invite federation inbound
      handlers, the §25.6 space sync receiver, ``SpaceRsvpMirrorBridge``,
      the schedule-poll bridge, ICS / AI import (``_as_datetime``, UTC).
      ``backup_service._import_table`` inserts raw rows, so a restore now
      normalises these two columns with the same rules first (an old
      backup's date-only bound must not abort the restore).
  (2) Non-migration alternatives considered and rejected: read-time
      normalisation in the repo (#994's first revision — the bad rows stay
      on disk, every reader must remember the rule, and the string-compared
      range filter still sees the raw shape); waiting for the event to be
      edited (most never are); a CHECK constraint (SQLite can't add one
      without rebuilding both tables — a trigger gives the same guarantee
      for every writer with no rebuild).
  (3) Smallest change: an UPDATE of only the offending values (offset-
      carrying and tombstone values untouched) plus four triggers. No
      column, no index, no table rebuild.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

_TABLES = ("calendar_events", "space_calendar_events")

#: A tz-aware ISO 8601 datetime: a date, a ``T`` (or space) and a time,
#: ending in an offset or ``Z``. GLOB keeps it to one SQLite expression.
_VALID = (
    "({c} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][T ][0-9][0-9]:*'"
    " AND ({c} GLOB '*[+-][0-9][0-9]:[0-9][0-9]' OR {c} GLOB '*Z'))"
)
_MESSAGE = "calendar start_dt/end_dt must be a tz-aware ISO 8601 datetime"


def _zone(tz: str | None) -> ZoneInfo | timezone:
    try:
        return ZoneInfo(tz or "UTC")
    except ZoneInfoNotFoundError, ValueError, OSError:
        return UTC


def _canonical(value: object, tz: str | None, *, is_end: bool) -> object:
    """The repaired bound, or ``value`` itself when it needs no repair."""
    if not isinstance(value, str) or value == "":
        return value
    try:
        day = date.fromisoformat(value)
    except ValueError:
        day = None
    if day is not None:
        if is_end:
            day += timedelta(days=1)
        local = datetime.combine(day, time(0, 0)).replace(tzinfo=_zone(tz), fold=0)
        return local.astimezone(UTC).isoformat()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        log.warning("0101: unparseable calendar bound %r left as stored", value)
        return value
    if parsed.tzinfo is not None:
        return value
    return parsed.replace(tzinfo=UTC).isoformat()


def _repair(conn: sqlite3.Connection, table: str) -> int:
    fixed = 0
    rows = conn.execute(f"SELECT rowid, start_dt, end_dt, tz FROM {table}").fetchall()
    for rowid, start, end, tz in rows:
        new_start = _canonical(start, tz, is_end=False)
        new_end = _canonical(end, tz, is_end=True)
        if new_start != start or new_end != end:
            conn.execute(
                f"UPDATE {table} SET start_dt=?, end_dt=? WHERE rowid=?",
                (new_start, new_end, rowid),
            )
            fixed += 1
    return fixed


def _install_guard(conn: sqlite3.Connection, table: str) -> None:
    ok_start = _VALID.format(c="NEW.start_dt")
    ok_end = _VALID.format(c="NEW.end_dt")
    if table == "space_calendar_events":
        # A tombstone stub (0085) blanks its bounds.
        stub = "(NEW.deleted_at IS NOT NULL AND NEW.{c} = '')"
        ok_start = f"({ok_start} OR {stub.format(c='start_dt')})"
        ok_end = f"({ok_end} OR {stub.format(c='end_dt')})"
    when = f"NOT {ok_start} OR NOT {ok_end}"
    for suffix, event in (
        ("insert", f"BEFORE INSERT ON {table}"),
        ("update", f"BEFORE UPDATE OF start_dt, end_dt ON {table}"),
    ):
        name = f"{table}_bounds_tz_{suffix}_check"
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.execute(
            f"CREATE TRIGGER {name} {event} WHEN {when}"
            f" BEGIN SELECT RAISE(ABORT, '{_MESSAGE}'); END"
        )


def migrate(conn: sqlite3.Connection) -> None:
    # Repair first, then the guard — the triggers must never see the old rows.
    for table in _TABLES:
        fixed = _repair(conn, table)
        if fixed:
            log.info(
                "0101: repaired %d %s row(s) with offset-less bounds", fixed, table
            )
    for table in _TABLES:
        _install_guard(conn, table)
