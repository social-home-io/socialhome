"""Calendar-related domain types (§5.2).

A :class:`Calendar` is a named collection of :class:`CalendarEvent` records
owned either by a user (``calendar_type='personal'``) or by a space
(``'space'``). Events may be "mirrored" — a copy of an event on another
calendar — in which case ``mirrored_from`` points to the source event id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True, frozen=True)
class Calendar:
    id: str
    name: str
    color: str  # hex, e.g. "#4a90e2"
    owner_username: str
    calendar_type: str = "personal"  # "personal" | "space"


@dataclass(slots=True, frozen=True)
class CalendarEvent:
    id: str
    calendar_id: str
    summary: str
    start: datetime
    end: datetime
    created_by: str  # user_id

    description: str | None = None
    all_day: bool = False
    attendees: tuple[str, ...] = ()  # user_ids
    mirrored_from: str | None = None  # source event id if this is a mirror

    #: RFC 5545 ``RRULE`` string (e.g. ``FREQ=WEEKLY;BYDAY=MO,WE``).
    #: ``None`` for one-off events. When set, ``start`` / ``end`` define
    #: the first occurrence's window; ``list_events_in_range`` expands
    #: additional virtual occurrences on the fly (§17.2).
    rrule: str | None = None


@dataclass(slots=True, frozen=True)
class CalendarEventCreate:
    """Input payload for ``POST /api/calendars/{id}/events``."""

    summary: str
    start: datetime
    end: datetime
    all_day: bool = False
    description: str | None = None
    attendees: tuple[str, ...] = field(default_factory=tuple)
    rrule: str | None = None


@dataclass(slots=True, frozen=True)
class CalendarEventUpdate:
    """Partial update payload for ``PATCH /api/calendars/events/{id}``.

    ``None`` for a field means "no change".
    """

    summary: str | None = None
    description: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    all_day: bool | None = None
    attendees: tuple[str, ...] | None = None
    rrule: str | None = None


class RSVPStatus:
    """Canonical RSVP status strings (see migration 0024_add_calendar_rsvp)."""

    GOING = "going"
    MAYBE = "maybe"
    DECLINED = "declined"

    ALL = frozenset({GOING, MAYBE, DECLINED})


@dataclass(slots=True, frozen=True)
class CalendarRSVP:
    event_id: str
    user_id: str
    status: str  # one of RSVPStatus.ALL
    updated_at: str  # ISO-8601
