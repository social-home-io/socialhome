"""The household wall clock the dashboard shows (lock screen, later the rail).

Time and date are rendered on the server in the *household's* time zone
(``HouseholdPreferences.tz``) — not the tablet's, which may be set to
anything. The date is spelled from the backend i18n catalog
(``dashboard.date.*``) in the dashboard locale; the time is always the
24-hour ``HH:MM`` (``lock.js`` keeps it ticking with the same format).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

__all__ = ["WallClock", "household_clock", "resolve_zone"]


@dataclass(slots=True, frozen=True)
class WallClock:
    """One reading of the household clock, ready for a template."""

    #: ``"09:05"`` (24-hour).
    time: str
    #: The long, localised date (``"Friday, January 15"``).
    date: str
    #: ``YYYY-MM-DD`` in the household zone — the page reloads when it
    #: changes, so the server re-spells the date.
    day: str
    #: ISO 8601 with offset, for ``<time datetime>``.
    iso: str
    #: The zone actually used (``"UTC"`` after a fallback).
    tz: str
    #: The reading as unix seconds — ``lock.js`` ticks from it, so a
    #: tablet whose own clock is off still shows the household time.
    unix: int


def resolve_zone(name: object) -> tzinfo:
    """``ZoneInfo(name)``, or UTC when ``name`` is no known IANA zone.

    Sync (the first lookup of a zone reads tzdata): call it through
    :func:`asyncio.to_thread` from a request.
    """
    if isinstance(name, str) and name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError, ValueError:
            log.warning("household time zone %r is unknown — using UTC", name)
    return UTC


def household_clock(now: float, zone: tzinfo, t: Callable[..., str]) -> WallClock:
    """The household clock at unix time ``now`` in ``zone``; ``t`` is a
    translator bound to the dashboard locale (``t(key, **fmt)``)."""
    local = datetime.fromtimestamp(now, zone)
    date = t(
        "dashboard.date.long",
        weekday=t(f"dashboard.date.weekday.{local.weekday()}"),
        month=t(f"dashboard.date.month.{local.month}"),
        day=local.day,
    )
    return WallClock(
        time=local.strftime("%H:%M"),
        date=date,
        day=local.date().isoformat(),
        iso=local.isoformat(timespec="seconds"),
        tz=str(zone),
        unix=int(now),
    )
