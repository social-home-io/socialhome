"""Deleted space timetables exporter — a space's timetable tombstones
(§25.6, v_92). Ships before ``timetables`` in :data:`RESOURCE_ORDER`; see
:mod:`.row_tombstones` for the shared shape.

A deleted space timetable keeps its row (``deleted_at`` set, content
dropped — migration 0063), so the tombstones are already on disk: a
household that missed the live ``SPACE_TIMETABLE_DELETED`` drops its copy
on the host's next stream. Streamed only to a v_92 requester (the provider
gates it), and the receiver takes it from the space's host alone — the
deleting moderator is not recorded on the row, so no other household's
copy can be judged by the live rule."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .row_tombstones import RowTombstonesExporter

if TYPE_CHECKING:
    from .....repositories.timetable_repo import AbstractSpaceTimetableRepo


class TimetablesDeletedExporter(RowTombstonesExporter):
    resource = "timetables_deleted"
    owner_key = "created_by"

    __slots__ = ()

    def __init__(self, space_timetable_repo: AbstractSpaceTimetableRepo) -> None:
        super().__init__(space_timetable_repo.list_tombstones_page)
