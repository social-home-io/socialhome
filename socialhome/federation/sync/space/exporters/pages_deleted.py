"""Deleted pages exporter — a space's page tombstones.

A space page delete keeps its row as a tombstone (migration 0073).
Streaming the tombstones is how a household that missed
``SPACE_PAGE_DELETED`` (offline past the outbox, or the envelope lost)
learns of the delete: the receiver tombstones its copy, and — from the
host — records a stub for an id it never held, so no stale copy streamed
later can create it. A separate resource rather than a flag on ``pages``
records, so an older receiver drops it as unknown instead of reading a
tombstone as a live page. Ships before ``pages`` in :data:`RESOURCE_ORDER`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .....domain.page import page_tombstone_to_wire_dict

if TYPE_CHECKING:
    from .....repositories.page_repo import AbstractPageRepo


#: Newest deletes first; a space that has deleted more pages than this
#: streams the most recent ones. Trade-off: a household that missed more
#: than this many deletes in one outage keeps the older pages — bounded
#: chunks over an unrealistic workload. Tombstones are never pruned.
MAX_TOMBSTONES_STREAMED: int = 500


class PagesDeletedExporter:
    resource = "pages_deleted"

    __slots__ = ("_repo",)

    def __init__(self, page_repo: "AbstractPageRepo") -> None:
        self._repo = page_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        tombstones = await self._repo.list_page_tombstones(
            space_id, limit=MAX_TOMBSTONES_STREAMED
        )
        return [page_tombstone_to_wire_dict(t, space_id) for t in tombstones]
