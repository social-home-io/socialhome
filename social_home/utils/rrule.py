"""Tiny subset of RFC 5545 RRULE expansion (§17.2).

Supports the four frequencies that account for ~95% of calendar
recurrences in practice:

* ``FREQ=DAILY;INTERVAL=N``
* ``FREQ=WEEKLY[;INTERVAL=N][;BYDAY=MO,TU,…]``
* ``FREQ=MONTHLY[;INTERVAL=N]``  (same day-of-month as the seed)
* ``FREQ=YEARLY[;INTERVAL=N]``   (same month+day as the seed)

Terminators: ``COUNT=N`` (inclusive of the seed) or ``UNTIL=YYYYMMDDTHHMMSSZ``.
Unrecognised rules emit a single seed occurrence so the event is never
lost — callers treat a non-recurring event the same way.

Keeping the parser deliberately tiny avoids the ~200 kB ``python-dateutil``
dependency and mirrors the ICS-import spirit (accept, then round-trip).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


_WEEKDAYS = {
    "MO": 0,
    "TU": 1,
    "WE": 2,
    "TH": 3,
    "FR": 4,
    "SA": 5,
    "SU": 6,
}


def _parse_until(value: str) -> datetime | None:
    """Accepts ``YYYYMMDDTHHMMSSZ``, ``YYYYMMDD``, or ISO-8601."""
    v = value.strip()
    if not v:
        return None
    # Compact RFC 5545 form: ``20260515T080000Z``
    if "T" in v and v.endswith("Z") and "-" not in v:
        try:
            return datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc,
            )
        except ValueError:
            return None
    if len(v) == 8 and v.isdigit():
        try:
            return datetime.strptime(v, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_rrule(rrule: str) -> dict:
    """Return a dict of ``{FREQ, INTERVAL, BYDAY, COUNT, UNTIL}``.

    All keys are present so callers don't have to ``.get(...)`` defaults.
    Unknown tokens are silently dropped; a malformed string yields
    ``{"FREQ": None}`` and the expander degenerates to "seed only".
    """
    out: dict = {
        "FREQ": None,
        "INTERVAL": 1,
        "BYDAY": [],
        "COUNT": None,
        "UNTIL": None,
    }
    if not rrule:
        return out
    for chunk in rrule.split(";"):
        if "=" not in chunk:
            continue
        k, _, v = chunk.partition("=")
        k = k.strip().upper()
        v = v.strip()
        if k == "FREQ":
            out["FREQ"] = (
                v.upper()
                if v.upper()
                in (
                    "DAILY",
                    "WEEKLY",
                    "MONTHLY",
                    "YEARLY",
                )
                else None
            )
        elif k == "INTERVAL":
            try:
                out["INTERVAL"] = max(1, int(v))
            except ValueError:
                pass
        elif k == "BYDAY":
            days = []
            for d in v.split(","):
                d = d.strip().upper()
                if d in _WEEKDAYS:
                    days.append(d)
            out["BYDAY"] = days
        elif k == "COUNT":
            try:
                out["COUNT"] = max(0, int(v))
            except ValueError:
                pass
        elif k == "UNTIL":
            parsed = _parse_until(v)
            if parsed is not None:
                out["UNTIL"] = parsed
    return out


def expand_rrule(
    seed_start: datetime,
    seed_end: datetime,
    rrule: str | None,
    *,
    window_start: datetime,
    window_end: datetime,
    safety_cap: int = 1000,
) -> list[tuple[datetime, datetime]]:
    """Expand a recurring event into a list of ``(start, end)`` pairs
    that intersect ``[window_start, window_end)``.

    Seeds are always considered (even when not in window) so a one-off
    event is returned verbatim. ``safety_cap`` prevents runaway loops
    on pathological rules.
    """
    duration = seed_end - seed_start
    rule = parse_rrule(rrule or "")
    freq = rule["FREQ"]
    interval = rule["INTERVAL"]
    count = rule["COUNT"]
    until = rule["UNTIL"]
    byday = rule["BYDAY"]

    if freq is None:
        # No recurrence — single occurrence.
        if seed_start < window_end and seed_end > window_start:
            return [(seed_start, seed_end)]
        return []

    occurrences: list[tuple[datetime, datetime]] = []
    emitted = 0

    def _push(s: datetime) -> bool:
        nonlocal emitted
        e = s + duration
        if s >= window_end:
            return False
        if e > window_start:
            occurrences.append((s, e))
        emitted += 1
        return True

    def _terminator_hit(s: datetime) -> bool:
        if count is not None and emitted >= count:
            return True
        if until is not None and s > until:
            return True
        return False

    if freq == "DAILY":
        step = timedelta(days=interval)
        cur = seed_start
        for _ in range(safety_cap):
            if _terminator_hit(cur):
                break
            if not _push(cur):
                break
            cur = cur + step
    elif freq == "WEEKLY":
        step = timedelta(weeks=interval)
        if not byday:
            byday_idx = [seed_start.weekday()]
        else:
            byday_idx = [_WEEKDAYS[d] for d in byday]
        # Advance week-by-week, emitting each matching day-of-week.
        week_start = seed_start - timedelta(days=seed_start.weekday())
        for _ in range(safety_cap):
            for dow in sorted(byday_idx):
                candidate = week_start + timedelta(days=dow)
                # Align time-of-day with the seed.
                candidate = candidate.replace(
                    hour=seed_start.hour,
                    minute=seed_start.minute,
                    second=seed_start.second,
                    microsecond=seed_start.microsecond,
                )
                if candidate < seed_start:
                    continue
                if _terminator_hit(candidate):
                    return occurrences
                if not _push(candidate):
                    return occurrences
            week_start += step
    elif freq == "MONTHLY":
        cur = seed_start
        month_step = interval
        for _ in range(safety_cap):
            if _terminator_hit(cur):
                break
            if not _push(cur):
                break
            m = cur.month - 1 + month_step
            y = cur.year + m // 12
            new_month = m % 12 + 1
            # Guard end-of-month edge cases (e.g. Jan 31 → Feb has no 31).
            try:
                cur = cur.replace(year=y, month=new_month)
            except ValueError:
                # Skip months that don't contain the seed day.
                # Find the nearest valid day <= seed day.
                for fallback in range(cur.day - 1, 27, -1):
                    try:
                        cur = cur.replace(
                            year=y,
                            month=new_month,
                            day=fallback,
                        )
                        break
                    except ValueError:
                        continue
                else:
                    break
    elif freq == "YEARLY":
        cur = seed_start
        year_step = interval
        for _ in range(safety_cap):
            if _terminator_hit(cur):
                break
            if not _push(cur):
                break
            try:
                cur = cur.replace(year=cur.year + year_step)
            except ValueError:  # Feb 29 on a non-leap year
                cur = cur.replace(year=cur.year + year_step, day=28)

    return occurrences
