"""Overtaking space-album deletes that cannot be stored as a tombstone row.

A ``SPACE_GALLERY_ALBUM_DELETED`` for an album not held here yet (the delete
overtook its create) is remembered so the create does not bring the album
back. For an id owner-bound (v_34) to the payload's owner in the space that
is a durable tombstone row (migration 0085, ``gallery_albums.deleted_at``).
Two kinds of delete cannot prove an id is this space's, so a durable row
for them could squat another space's album id on this household for good:

* a **legacy** (unbound, uuid4) album id — pre-v_34 albums, and
* an **owner-less** delete of a bound id — a v_33 sender's, which names no
  ``owner_user_id`` to check the id against (accepted from a moderator
  household, as v_33 did).

Those are remembered here instead: a bounded, in-memory set of
``(space_id, album_id)`` that the live create path and the §25.6 sync
receiver consult — the protection the pre-0085 ``GalleryAlbumTombstones``
gave every overtaking delete, with the same scope (restart-scoped, capped,
oldest first out). Album ids are random, so a recorded id is never
legitimately re-created.
"""

from __future__ import annotations

from collections import OrderedDict

#: Plenty for any realistic rate of overtaking legacy deletes between
#: restarts; bounded so a flood of deletes cannot grow memory without limit.
MAX_ENTRIES: int = 4096


class LegacyAlbumDeletes:
    """Bounded record of overtaking album deletes no tombstone row proves."""

    __slots__ = ("_entries", "_max")

    def __init__(self, *, max_entries: int = MAX_ENTRIES) -> None:
        self._entries: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._max = max_entries

    def record(self, space_id: str, album_id: str) -> None:
        key = (space_id, album_id)
        self._entries.pop(key, None)
        self._entries[key] = None
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def is_deleted(self, space_id: str, album_id: str) -> bool:
        return (space_id, album_id) in self._entries
