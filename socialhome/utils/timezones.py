"""IANA timezone validation — one shared check for every tz boundary.

Calendar rows carry an IANA zone name (``calendar_events.tz``) that the
SPA feeds straight to ``Intl``. ``Intl`` raises ``RangeError`` on an
unknown name, and a single bad row takes the whole calendar view down
with it — so every zone name entering the system (an API body, a
federation payload) is validated against the local tz database before
it is persisted.

Two entry points, matching the two policies already in the codebase:

* :func:`is_valid_tz` — a bare predicate, for call sites that have their
  own fallback chain (``CalendarService._resolve_personal_tz``) or that
  reject with a domain error (``update_event``).
* :func:`coerce_tz` — validate-or-``"UTC"``, for untrusted input where
  the event must still land. Fails closed on the *value*, not the event.

Plus the wall-clock ↔ instant helpers every "today in zone X" caller
shares: :func:`zone_of`, :func:`local_date`, :func:`local_instant`,
:func:`local_day_bounds`.
"""

import logging
from datetime import UTC, date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

#: Anchor used whenever a zone name can't be trusted or resolved. Also
#: the ``NOT NULL DEFAULT`` of every ``tz`` column (``0002_calendar_
#: timezone.sql``), so this is the value a pre-tz row already reads back.
DEFAULT_TZ = "UTC"


def is_valid_tz(value: str) -> bool:
    """Return whether ``value`` names a zone in the local IANA database.

    ``ZoneInfo`` raises ``ZoneInfoNotFoundError`` (a ``KeyError``) for an
    unknown-but-well-formed key like ``"Foo/Bar"`` and ``ValueError`` for
    a malformed one (absolute or ``..``-containing paths) — both are just
    "not a zone" here.
    """
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        # Well-formed key that isn't in the database ("Foo/Bar").
        return False
    except ValueError:
        # Malformed key — absolute or ``..``-containing path.
        return False
    except OSError:
        # The key maps to a filesystem path; an overlong one raises
        # ENAMETOOLONG from the tzdata lookup.
        return False
    return True


def coerce_tz(value: object, *, context: str) -> str:
    """Validate an untrusted zone name, falling back to :data:`DEFAULT_TZ`.

    Used at trust boundaries (federation inbound) where a buggy or
    hostile peer can put an arbitrary string on the wire. A rejected
    value logs at WARNING with ``context`` (event type + sending
    instance) so a misbehaving peer is diagnosable rather than silent.
    """
    name = str(value or DEFAULT_TZ)
    if is_valid_tz(name):
        return name
    log.warning(
        "%s carried unknown IANA timezone %r — anchoring the row to %s",
        context,
        name,
        DEFAULT_TZ,
    )
    return DEFAULT_TZ


def zone_of(tz: str) -> ZoneInfo | timezone:
    """The zone for an IANA name — UTC when it is unknown."""
    return ZoneInfo(tz) if is_valid_tz(tz) else UTC


def local_date(now: datetime, tz: str) -> date:
    """The calendar date of the instant ``now`` in ``tz``."""
    return now.astimezone(zone_of(tz)).date()


def local_instant(d: date, t: time, tz: str) -> datetime:
    """The UTC instant of wall-clock ``t`` on ``d`` in ``tz``.

    Deterministic across DST changes: an ambiguous time (the autumn
    fold) takes its first occurrence (``fold=0``); a nonexistent time
    (the spring gap) is shifted forward by the gap — zoneinfo applies
    the pre-transition offset, so 02:30 in a 02:00→03:00 gap becomes
    03:30 local.
    """
    local = datetime.combine(d, t).replace(tzinfo=zone_of(tz), fold=0)
    return local.astimezone(UTC)


def local_day_bounds(d: date, tz: str) -> tuple[datetime, datetime]:
    """The UTC instants of local midnight starting ``d`` and the next day
    in ``tz`` — the half-open window ``[start, end)`` of that calendar day
    (23 or 25 hours long across a DST change)."""
    return (
        local_instant(d, time(0, 0), tz),
        local_instant(d + timedelta(days=1), time(0, 0), tz),
    )


def parse_event_bound(value: str, *, tz: str, is_end: bool) -> datetime:
    """Parse a calendar event's ``start`` / ``end`` from an API body into
    the tz-aware UTC instant the ``start_dt`` / ``end_dt`` columns store.

    * A date-only value (``"2026-10-20"``) names a day in the event's
      ``tz``: as a start it is that day's local midnight, as an end the
      next local midnight — the day is included, so ``start == end ==
      "2026-10-20"`` is the one all-day "20 Oct". This is the same
      exclusive-midnight bound the SPA reads back in ``tz``.
    * A datetime without an offset is UTC (the codebase-wide rule).
    * A datetime with an offset is converted to UTC.

    Raises :class:`ValueError` (→ 422) on anything else.
    """
    try:
        day = date.fromisoformat(value)
    except ValueError:
        day = None
    if day is not None:
        return local_day_bounds(day, tz)[1 if is_end else 0]
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid datetime: {value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_all_day_bound(value: str, *, tz: str, is_end: bool) -> datetime:
    """Parse an ALL-DAY event's bound that came WITHOUT an explicit ``tz``.

    An all-day event names days, not instants, and without a ``tz`` the
    request says nothing about which zone its instants were meant in —
    the resolved ``tz`` (creator → household → UTC) is a fallback, not the
    author's clock. So the value names the day *as written*:

    * a date-only value is that day (exactly :func:`parse_event_bound`);
    * a datetime is read on its own wall clock — in its own offset, or as
      written when it has none — and only its date counts.

    That day is then anchored to ``tz`` like a date-only bound: a start is
    that day's local midnight; an end is the next local midnight after its
    wall clock (an exclusive ``00:00`` end stays on that midnight, so
    ``…T23:59`` and the next day's ``T00:00`` both end the same day).

    With an explicit ``tz`` the caller uses :func:`parse_event_bound` —
    the SPA sends UTC instants of local ``00:00`` / ``23:59`` in that
    zone, which read back exactly. Raises :class:`ValueError` (→ 422).
    """
    try:
        day = date.fromisoformat(value)
    except ValueError:
        day = None
    if day is not None:
        return local_day_bounds(day, tz)[1 if is_end else 0]
    try:
        wall = datetime.fromisoformat(value).replace(tzinfo=None)
    except ValueError as exc:
        raise ValueError(f"invalid datetime: {value!r}") from exc
    day = wall.date()
    if is_end and wall.time() != time(0, 0):
        day += timedelta(days=1)
    return local_instant(day, time(0, 0), tz)


def canonical_event_bound(value: object, *, tz: str | None, is_end: bool) -> object:
    """Bring a STORED-shape bound (a backup row) to the column's invariant.

    An offset-less value (date-only or a naive datetime) is rewritten with
    :func:`parse_event_bound`'s rules in ``tz`` and returned as its
    ``+00:00`` ISO string; everything else — a value with an offset, the
    ``''`` of a tombstone stub, a non-string, an unparseable string — is
    returned unchanged (migration 0100's triggers judge the latter).
    """
    if not isinstance(value, str) or value == "":
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is not None:
        return value
    return parse_event_bound(value, tz=tz or DEFAULT_TZ, is_end=is_end).isoformat()
