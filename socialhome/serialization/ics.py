"""RFC 5545 (iCalendar) serializer for space calendar events (Phase F).

Hand-written serializer; we don't depend on the ``icalendar`` package
for the writer side because the surface we emit is small and stable.
The reader side (in :mod:`services.calendar_import_service`) does
already use ``icalendar`` for AI imports, so adding it as a runtime
dep gains nothing.

Limits:

* Only the four FREQ values our :func:`utils.rrule.expand_rrule`
  supports are emitted (DAILY/WEEKLY/MONTHLY/YEARLY) — the stored
  rrule string is passed through verbatim, so any other rule that
  arrived via a federation peer round-trips even if our expander
  treats it as a one-off.
* No ATTENDEE lines: cross-instance member identity is fuzzy enough
  that leaking handles into a third-party calendar app crosses a
  privacy line. RSVPs stay in-app.
* No VTIMEZONE block — timestamps are emitted in UTC (``...Z``); the
  client renders local time from there.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from typing import Iterable

from ..domain.calendar import CalendarEvent, EventReminder

#: Conservative line-folding length per RFC 5545 §3.1 (max 75 octets).
MAX_LINE = 75
#: ``PRODID`` shipped on every emitted VCALENDAR. Acts as a tracer in
#: support cases — calendar clients sometimes log the PRODID.
PRODID = "-//Social Home//Calendar//EN"


def serialize_event(
    event: CalendarEvent,
    *,
    reminders: Iterable[EventReminder] = (),
    cancelled: bool = False,
) -> bytes:
    """Serialize one event as a complete VCALENDAR document.

    ``reminders`` are emitted as VALARM blocks so the user's calendar
    app honours their configured offsets. Pass an empty iterable to skip.
    """
    return _wrap_calendar([_event_block(event, list(reminders), cancelled)])


def serialize_feed(
    events: Iterable[CalendarEvent],
    *,
    reminders_by_event: dict[str, list[EventReminder]] | None = None,
) -> bytes:
    """Serialize a collection of events as one VCALENDAR document.

    Used by the per-(user, space) subscribable feed. ``reminders_by_event``
    maps ``event.id`` to that user's reminders so each VEVENT carries the
    matching VALARMs.
    """
    rby = reminders_by_event or {}
    blocks = [_event_block(ev, rby.get(ev.id, []), cancelled=False) for ev in events]
    return _wrap_calendar(blocks)


def feed_etag(payload: bytes) -> str:
    """Strong ETag for ``Cache-Control`` on the feed endpoint.

    sha256-hash of the body — calendar clients honour ETag and do
    conditional GETs, saving bandwidth on the polling cycle (Apple
    Calendar polls every ~5 min by default).
    """
    return f'"{hashlib.sha256(payload).hexdigest()[:32]}"'


# ─── Internals ──────────────────────────────────────────────────────────────


def _wrap_calendar(blocks: list[list[str]]) -> bytes:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}"]
    for block in blocks:
        lines.extend(block)
    lines.append("END:VCALENDAR")
    out = "\r\n".join(_fold(line) for line in lines) + "\r\n"
    return out.encode("utf-8")


def _event_block(
    event: CalendarEvent,
    reminders: list[EventReminder],
    cancelled: bool,
) -> list[str]:
    lines: list[str] = ["BEGIN:VEVENT"]
    lines.append(f"UID:{_escape(event.id)}")
    lines.append(f"DTSTAMP:{_dt(datetime.now(timezone.utc))}")
    if event.all_day:
        lines.append(f"DTSTART;VALUE=DATE:{_d(event.start)}")
        lines.append(f"DTEND;VALUE=DATE:{_d(event.end)}")
    else:
        lines.append(f"DTSTART:{_dt(event.start)}")
        lines.append(f"DTEND:{_dt(event.end)}")
    lines.append(f"SUMMARY:{_escape(event.summary)}")
    if event.description:
        lines.append(f"DESCRIPTION:{_escape(event.description)}")
    if event.rrule:
        # The stored RRULE is already RFC 5545 — pass through verbatim.
        lines.append(f"RRULE:{event.rrule}")
    if cancelled:
        lines.append("STATUS:CANCELLED")
    for r in reminders:
        lines.append("BEGIN:VALARM")
        lines.append("ACTION:DISPLAY")
        lines.append(f"DESCRIPTION:{_escape(event.summary)}")
        lines.append(f"TRIGGER:-PT{int(r.minutes_before)}M")
        lines.append("END:VALARM")
    lines.append("END:VEVENT")
    return lines


def _dt(value: datetime) -> str:
    """``YYYYMMDDTHHMMSSZ`` form per RFC 5545."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.strftime("%Y%m%dT%H%M%SZ")


def _d(value: datetime | date) -> str:
    """``YYYYMMDD`` form for VALUE=DATE."""
    if isinstance(value, datetime):
        value = value.date()
    return value.strftime("%Y%m%d")


def _escape(value: str) -> str:
    """Per RFC 5545 §3.3.11 — escape backslash, comma, semicolon, newline."""
    return (
        value.replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("\n", "\\n")
        .replace("\r", "")
    )


def _fold(line: str) -> str:
    """RFC 5545 §3.1 line folding — split lines >75 octets into a
    continuation prefixed by a single space.

    Folding operates on octets, not characters; for ASCII content the
    distinction doesn't matter, but for non-ASCII summaries a slightly
    conservative split keeps us under the limit. We measure UTF-8 bytes
    and split at codepoint boundaries to stay valid.
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= MAX_LINE:
        return line
    out: list[str] = []
    current = ""
    current_bytes = 0
    for ch in line:
        ch_bytes = len(ch.encode("utf-8"))
        if current_bytes + ch_bytes > MAX_LINE:
            out.append(current)
            current = " " + ch
            current_bytes = 1 + ch_bytes
        else:
            current += ch
            current_bytes += ch_bytes
    out.append(current)
    return "\r\n".join(out)
