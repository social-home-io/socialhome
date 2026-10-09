"""Shared shape of the §25.6 tombstone resources of migration 0085.

Space stickies, calendar events, gallery albums / items and zones keep
their row when deleted (content blanked, ``deleted_at`` / ``deleted_by``
set). Each streams its tombstones as its own ``*_deleted`` resource,
ordered before the live one, so a household that missed the live
``*_DELETED`` event (offline past the outbox, a mesh drop) applies the
delete — and, from the host, records a stub for an id it never held, so no
stale copy streamed later can create it. Without them a household that
missed a delete kept the row forever and, as a catch-up provider,
re-spread it to every joiner.

A record names the row, never its content: ``id``, the owner under the
live resource's own key (``author`` / ``created_by`` / ``owner_user_id``
/ ``uploaded_by``), ``created_at``, the album of a gallery item, and
``actor_user_id`` — the user who made the delete — when one is recorded.
The owner binds an owner-bound id to the space on the receiver and is what
the live delete's authority rule judges; the actor is what the space's
access level judges. A separate resource rather than a flag on the live
records, so an older receiver drops it as unknown instead of reading a
tombstone as a live row.

No retention window: every tombstone streams, page by page (keyset on the
row id). None of these types is swept by retention, and a tombstone is a
few dozen bytes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from .....domain.tombstone import SpaceRowTombstone
from ..exporter import PagedExporterMixin
from ..window import SYNC_PAGE_SIZE, iter_pages

#: A repo's ``list_*_tombstones_page(space_id, *, cursor, limit)``.
TombstonePageFetch = Callable[
    ..., Awaitable[tuple[list[SpaceRowTombstone], int | None]]
]


class RowTombstonesExporter(PagedExporterMixin):
    """Stream one space's row tombstones of one type.

    Subclasses set :attr:`resource`, :attr:`owner_key` (the live record's
    key for the row's creator) and, for a gallery item, :attr:`parent_key`.
    """

    resource: str = ""
    owner_key: str = ""
    parent_key: str | None = None

    __slots__ = ("_fetch",)

    def __init__(self, fetch: TombstonePageFetch) -> None:
        self._fetch = fetch

    async def iter_batches(self, space_id: str) -> AsyncIterator[list[dict[str, Any]]]:
        async def page(
            cursor: int | None,
        ) -> tuple[list[SpaceRowTombstone], int | None]:
            return await self._fetch(space_id, cursor=cursor, limit=SYNC_PAGE_SIZE)

        async for rows in iter_pages(page):
            yield [self.record(t) for t in rows]

    def record(self, tombstone: SpaceRowTombstone) -> dict[str, Any]:
        """The wire record of a deleted row — identity only, never content."""
        record: dict[str, Any] = {
            "id": tombstone.id,
            self.owner_key: tombstone.owner,
            "created_at": tombstone.created_at,
        }
        if self.parent_key is not None:
            record[self.parent_key] = tombstone.parent_id
        if tombstone.deleted_by:
            record["actor_user_id"] = tombstone.deleted_by
        return record
