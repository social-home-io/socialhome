"""Space-timetable exporter — a space's live (non-deleted) timetables.

Streams each timetable as its domain wire dict
(:func:`socialhome.domain.timetable.to_wire_dict`, the same shape the live
``SPACE_TIMETABLE_UPSERTED`` event carries) so a member household joining
mid-life gets every timetable, not just the ones edited after its join.

Tombstones are not streamed — no other resource streams its deletes
either. A joiner that never held a timetable has nothing to delete; a
member that was offline for a delete gets it from the outbox retry of the
live ``SPACE_TIMETABLE_DELETED``; and a receiver that holds the tombstone
refuses any later copy of the id (the repo's last-writer-wins upsert never
touches a deleted row), so a stale provider cannot bring one back.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from .....domain.timetable import to_wire_dict

if TYPE_CHECKING:
    from .....repositories.timetable_repo import AbstractSpaceTimetableRepo


class TimetablesExporter:
    resource = "timetables"

    __slots__ = ("_repo",)

    def __init__(self, space_timetable_repo: "AbstractSpaceTimetableRepo") -> None:
        self._repo = space_timetable_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        return await self._records(space_id, None)

    async def iter_changed(
        self, space_id: str, since: int
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """The live timetables changed after ``since`` (§25.6 incremental,
        migration 0088)."""
        yield await self._records(space_id, since)

    async def _records(self, space_id: str, since: int | None) -> list[dict[str, Any]]:
        return [
            to_wire_dict(tt)
            for tt in await self._repo.list_by_space(space_id, since_seq=since)
        ]
