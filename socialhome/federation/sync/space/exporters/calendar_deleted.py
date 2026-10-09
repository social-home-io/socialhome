"""Deleted calendar events exporter — a space's event tombstones (§25.6,
migration 0085). Ships before ``calendar`` in :data:`RESOURCE_ORDER`; see
:mod:`.row_tombstones` for the shared shape. A recurring event is one row
(its occurrences expand on read, and a per-occurrence RSVP hangs off the
event), so its tombstone covers the whole series."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .row_tombstones import RowTombstonesExporter

if TYPE_CHECKING:
    from .....repositories.calendar_repo import AbstractSpaceCalendarRepo


class CalendarDeletedExporter(RowTombstonesExporter):
    resource = "calendar_deleted"
    owner_key = "created_by"

    __slots__ = ()

    def __init__(self, space_calendar_repo: "AbstractSpaceCalendarRepo") -> None:
        super().__init__(space_calendar_repo.list_event_tombstones_page)
