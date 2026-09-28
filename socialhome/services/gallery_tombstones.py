"""Recently deleted space gallery albums (§23.119, v_33).

An album delete leaves no row behind, so without a record of it two
orderings would bring a deleted album back:

* the delete overtakes the create (``SPACE_GALLERY_ALBUM_DELETED`` for an
  album not held here yet, then the ``_CREATED``), and
* a ``SPACE_SYNC_RESUME`` replay or a §25.6 sync from a household that
  missed the delete re-sends the album.

This keeps a bounded, in-memory set of ``(space_id, album_id)`` pairs with
the time of the delete. The receiver refuses to create a recorded album,
and the resume provider replays recorded deletes newer than the
requester's ``since``. Album ids are random (uuid4), so a recorded id is
never legitimately re-created. The record does not survive a restart —
deliberately: a tombstone table would be a migration for a window the
live events and the next sync already close in practice.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..domain.events import GalleryAlbumDeleted

if TYPE_CHECKING:
    from ..infrastructure.event_bus import EventBus

#: Plenty for any realistic delete rate between two syncs; bounded so a
#: flood of deletes cannot grow memory without limit.
MAX_TOMBSTONES: int = 4096


class GalleryAlbumTombstones:
    """Bounded record of space albums deleted during this process's life."""

    __slots__ = ("_entries", "_max")

    def __init__(self, *, max_entries: int = MAX_TOMBSTONES) -> None:
        self._entries: OrderedDict[tuple[str, str], datetime] = OrderedDict()
        self._max = max_entries

    def wire(self, bus: "EventBus") -> None:
        """Record every space album delete published on the bus."""
        bus.subscribe(GalleryAlbumDeleted, self._on_deleted)

    async def _on_deleted(self, event: GalleryAlbumDeleted) -> None:
        if event.space_id:
            self.record(event.space_id, event.album_id, at=event.occurred_at)

    def record(
        self, space_id: str, album_id: str, *, at: datetime | None = None
    ) -> None:
        key = (space_id, album_id)
        self._entries.pop(key, None)
        self._entries[key] = _utc(at or datetime.now(timezone.utc))
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def is_deleted(self, space_id: str, album_id: str) -> bool:
        return (space_id, album_id) in self._entries

    def deleted_since(self, space_id: str, since: str) -> list[str]:
        """Album ids of ``space_id`` deleted after ``since`` (ISO 8601).

        An unparsable ``since`` lists every recorded delete of the space —
        replaying a delete twice is harmless, missing one is not.
        """
        try:
            cutoff: datetime | None = _utc(datetime.fromisoformat(since))
        except ValueError:
            cutoff = None
        return [
            album_id
            for (sid, album_id), at in self._entries.items()
            if sid == space_id and (cutoff is None or at > cutoff)
        ]


def _utc(value: datetime) -> datetime:
    """Naive timestamps are UTC (the DB's ``datetime('now')`` shape)."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
