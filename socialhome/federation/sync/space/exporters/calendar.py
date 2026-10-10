"""Calendar exporter — a space's calendar events, as stored.

Every live event of the space streams, page by page, **as its row**: a
recurring event once, with its ``rrule`` (the receiver expands its
occurrences on read, as the host does), never as one record per
occurrence. And whatever its date — no window around "now": nothing prunes
calendar events (the retention sweep touches posts and chat only), so a
window would hide from a member events the space still shows, and one
relative to the clock would make a full and an incremental session
disagree as rows drift across its edge. Deletes ride ``calendar_deleted``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from typing import Any, TYPE_CHECKING

from .....domain.calendar import OCCURRENCE_ID_SEPARATOR, is_occurrence_id
from ..exporter import PagedExporterMixin
from ..window import SYNC_PAGE_SIZE, iter_pages

if TYPE_CHECKING:
    from .....domain.calendar import CalendarEvent
    from .....repositories.calendar_repo import AbstractSpaceCalendarRepo


class CalendarExporter(PagedExporterMixin):
    resource = "calendar"

    __slots__ = ("_repo",)

    def __init__(self, space_calendar_repo: "AbstractSpaceCalendarRepo") -> None:
        self._repo = space_calendar_repo

    def expanded(self) -> "ExpandedCalendarExporter":
        """The record set for a requester below v_56 (see there)."""
        return ExpandedCalendarExporter(self._repo)

    def iter_batches(self, space_id: str) -> AsyncIterator[list[dict[str, Any]]]:
        return self._pages(space_id, None)

    def iter_changed(
        self, space_id: str, since: int
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """The events changed after ``since`` (an incremental session).
        RSVPs are not part of the record, so an RSVP change streams nothing
        here."""
        return self._pages(space_id, since)

    async def _pages(
        self, space_id: str, since: int | None
    ) -> AsyncIterator[list[dict[str, Any]]]:
        async def page(cursor: int | None) -> tuple[list["CalendarEvent"], int | None]:
            return await self._repo.list_events_sync_page(
                space_id, cursor=cursor, limit=SYNC_PAGE_SIZE, since=since
            )

        async for events in iter_pages(page):
            yield [_event_to_dict(e) for e in events]


#: The window an older requester's record set is expanded in (the shape
#: before v_56): ~10 years either side of the provider's clock.
_EXPANDED_WINDOW = timedelta(days=3652)


class ExpandedCalendarExporter:
    """The ``calendar`` record set a requester below v_56 understands: every
    series expanded into its occurrences (``<id>@<start>`` records) in a
    ±10-year window around the provider's clock — the pre-v_56 shape.

    An older receiver reads no ``rrule`` from a record and upserts the row
    without one, so a record carrying a stored series row's own id would turn
    the series it holds into a one-off event. The expansion yields exactly
    such a record for a series' first occurrence (the stored row itself), so
    that one goes out under ``<id>@<start>`` too: an older receiver gets only
    occurrence records — which it stores as one-off events, as it always
    did, the first occurrence included — and its series row (delivered live,
    with its rule) is never overwritten. Same
    resource, so a requester that upgrades gets one full stream in the new
    shape (its version is part of the session shape)."""

    resource = "calendar"

    __slots__ = ("_repo",)

    def __init__(self, space_calendar_repo: "AbstractSpaceCalendarRepo") -> None:
        self._repo = space_calendar_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        return await self._records(space_id, None)

    async def iter_changed(
        self, space_id: str, since: int
    ) -> AsyncIterator[list[dict[str, Any]]]:
        yield await self._records(space_id, since)

    async def _records(self, space_id: str, since: int | None) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        events = await self._repo.list_events_in_range(
            space_id,
            start=now - _EXPANDED_WINDOW,
            end=now + _EXPANDED_WINDOW,
            since=since,
        )
        return [_event_to_dict(_as_occurrence(e)) for e in events]


def _as_occurrence(event: "CalendarEvent") -> "CalendarEvent":
    """The expansion yields a series' first occurrence as the stored series
    row itself; an older receiver would upsert it without its rule. It goes
    out as an occurrence like the others, under ``<id>@<start>``."""
    if event.rrule and not is_occurrence_id(event.id, event.start.isoformat()):
        return replace(
            event,
            id=f"{event.id}{OCCURRENCE_ID_SEPARATOR}{event.start.isoformat()}",
        )
    return event


def _event_to_dict(event: "CalendarEvent") -> dict[str, Any]:
    d = asdict(event)
    for field in ("start", "end"):
        v = d.get(field)
        if v is not None and not isinstance(v, str):
            d[field] = v.isoformat()
    d["attendees"] = list(d.get("attendees") or ())
    return d
