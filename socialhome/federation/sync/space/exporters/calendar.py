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
from dataclasses import asdict
from typing import Any, TYPE_CHECKING

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


def _event_to_dict(event: "CalendarEvent") -> dict[str, Any]:
    d = asdict(event)
    for field in ("start", "end"):
        v = d.get(field)
        if v is not None and not isinstance(v, str):
            d[field] = v.isoformat()
    d["attendees"] = list(d.get("attendees") or ())
    return d
