"""The household wall clock and the time phrases the dashboard shows.

Time and date are rendered on the server in the *household's* time zone
(``HouseholdPreferences.tz``) — not the tablet's, which may be set to
anything. The date is spelled from the backend i18n catalog
(``dashboard.date.*``) in the dashboard locale; the time is always the
24-hour ``HH:MM`` (``lock.js`` / ``dashboard.js`` keep it ticking with
the same format). Relative phrases ("in 28 min", "2 days late") come
from ``dashboard.rel.*`` / ``dashboard.chore.*`` ("4 min ago" too).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

__all__ = [
    "WallClock",
    "ago",
    "days_late",
    "due_label",
    "hhmm",
    "household_clock",
    "relative_in",
    "resolve_zone",
    "until",
]


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


def hhmm(value: datetime) -> str:
    """``value``'s wall-clock time, 24-hour ``HH:MM`` (in its own zone —
    snapshot datetimes are already in the household zone)."""
    return value.strftime("%H:%M")


def relative_in(now: datetime, then: datetime, t: Callable[..., str]) -> str:
    """ "in 28 min" / "in 1 h 35 min" / "in 4 h" — how long until ``then``.

    Minutes round up (a start 20 s away is "in 1 min", never "in 0
    min"); from three hours on only whole hours are said. ``then`` at
    or before ``now`` is "now".
    """
    minutes = math.ceil((then - now).total_seconds() / 60)
    if minutes <= 0:
        return t("dashboard.rel.now")
    if minutes < 60:
        return t("dashboard.rel.in_minutes", n=minutes)
    h, m = divmod(minutes, 60)
    if m == 0 or h >= 3:
        return t("dashboard.rel.in_hours", h=h)
    return t("dashboard.rel.in_hours_minutes", h=h, m=m)


def ago(
    now: datetime, then: datetime, t: Callable[..., str], *, hours: bool = False
) -> str:
    """ "just now" / "4 min ago" — how long since ``then`` (whole minutes,
    rounded down; ``then`` under a minute ago, or ahead of ``now``, is
    "just now"). With ``hours``, an hour or more is said in whole hours
    ("2 h ago", rounded down)."""
    minutes = math.floor((now - then).total_seconds() / 60)
    if minutes < 1:
        return t("dashboard.rel.just_now")
    if hours and minutes >= 60:
        return t("dashboard.rel.hours_ago", n=minutes // 60)
    return t("dashboard.rel.minutes_ago", n=minutes)


def until(end: datetime, t: Callable[..., str]) -> str:
    """ "until 17:00"."""
    return t("dashboard.rel.until", time=hhmm(end))


def days_late(days: int, t: Callable[..., str]) -> str:
    """ "1 day late" / "3 days late" for an overdue chore."""
    if days == 1:
        return t("dashboard.chore.late_one")
    return t("dashboard.chore.late_many", n=days)


def due_label(due: date | None, today: date, t: Callable[..., str]) -> str | None:
    """When a chore is due, as a tablet says it: "today", "tomorrow", a
    weekday within the coming week, else a short date ("October 30").
    ``None`` without a due date."""
    if due is None:
        return None
    days = (due - today).days
    if days == 0:
        return t("dashboard.chore.due_today")
    if days == 1:
        return t("dashboard.chore.due_tomorrow")
    if 1 < days < 7:
        return t(f"dashboard.date.weekday.{due.weekday()}")
    return t(
        "dashboard.date.short",
        month=t(f"dashboard.date.month.{due.month}"),
        day=due.day,
    )
