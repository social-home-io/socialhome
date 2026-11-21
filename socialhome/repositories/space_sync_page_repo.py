"""The newest sync page a household holds per space (migration 0151).

``docs/protocol/sync-pages.md``: one row per space, the same on the signer
and on a member — the signed header, its bucket vector and (signer only)
the delta since its previous page. Replaced by a newer page, deleted on an
authority rotation and with the space.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.space_sync_page import StoredSyncPage


@runtime_checkable
class AbstractSpaceSyncPageRepo(Protocol):
    async def get(self, space_id: str) -> StoredSyncPage | None:
        """The page held for the space, or ``None``."""
        ...

    async def put(self, page: StoredSyncPage) -> None:
        """Store *page* as the space's page (replacing the one held)."""
        ...

    async def delete(self, space_id: str) -> None:
        """Drop the space's page (an authority rotation)."""
        ...


class SqliteSpaceSyncPageRepo:
    """SQLite-backed :class:`AbstractSpaceSyncPageRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def get(self, space_id: str) -> StoredSyncPage | None:
        row = await self._db.fetchone(
            "SELECT page_json, vector, delta FROM space_sync_pages WHERE space_id=?",
            (space_id,),
        )
        if row is None:
            return None
        return StoredSyncPage(
            space_id=space_id,
            page_json=str(row["page_json"]),
            vector=bytes(row["vector"]),
            delta=bytes(row["delta"]) if row["delta"] is not None else None,
        )

    async def put(self, page: StoredSyncPage) -> None:
        # A space this household no longer holds has no row to hang the
        # page on: the FK makes the INSERT a no-op instead of an error.
        await self._db.enqueue(
            "INSERT INTO space_sync_pages(space_id, page_json, vector, delta)"
            " SELECT ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM spaces WHERE id=?)"
            " ON CONFLICT(space_id) DO UPDATE SET page_json=excluded.page_json,"
            " vector=excluded.vector, delta=excluded.delta",
            (page.space_id, page.page_json, page.vector, page.delta, page.space_id),
        )

    async def delete(self, space_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM space_sync_pages WHERE space_id=?", (space_id,)
        )


__all__ = ["AbstractSpaceSyncPageRepo", "SqliteSpaceSyncPageRepo"]
