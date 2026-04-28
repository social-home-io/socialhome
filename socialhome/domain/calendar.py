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
    #: Phase C: per-occurrence "going" capacity. ``None`` = no cap (the
    #: original three-state RSVP flow). ``int`` = max ``going`` RSVPs
    #: per occurrence; further requests become ``REQUESTED`` (pending
    #: approval) or ``WAITLIST``.
    capacity: int | None = None


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
    """Canonical RSVP status strings.

    ``GOING`` / ``MAYBE`` / ``DECLINED`` are the self-reported responses
    from the original three-state model. ``REQUESTED`` and ``WAITLIST``
    are added for capacity-limited events: when an event has
    ``capacity is not None`` the host approves requests, and overflow
    RSVPs land on the waitlist with auto-promotion when seats free up.
    """

    GOING = "going"
    MAYBE = "maybe"
    DECLINED = "declined"
    REQUESTED = "requested"
    WAITLIST = "waitlist"

    ALL = frozenset({GOING, MAYBE, DECLINED, REQUESTED, WAITLIST})

    #: Statuses a member can choose directly (the rest are host-driven
    #: transitions on capped events).
    USER_SETTABLE = frozenset({GOING, MAYBE, DECLINED})


@dataclass(slots=True, frozen=True)
class CalendarRSVP:
    """A single RSVP row for a (event, user, occurrence) triple.

    For non-recurring events ``occurrence_at`` equals ``event.start``;
    for recurring events it's the specific instance's start datetime.
    """

    event_id: str
    user_id: str
    status: str  # one of RSVPStatus.ALL
    updated_at: str  # ISO-8601
    occurrence_at: str = ""  # ISO-8601; empty only on legacy in-memory test fakes


@dataclass(slots=True, frozen=True)
class EventReminder:
    """Phase D: per-user reminder for a specific occurrence of an event.

    ``fire_at`` is the precomputed UTC ISO instant the scheduler should
    deliver the push notification. ``minutes_before`` is the user's
    chosen offset (e.g. 60 = "1 hour before"); 0 means at start.
    ``sent_at`` is filled in after the scheduler emits the notification
    — un-sent rows with ``fire_at <= now()`` are the scheduler's
    work-queue.
    """

    event_id: str
    user_id: str
    occurrence_at: str
    minutes_before: int
    fire_at: str
    sent_at: str | None = None
