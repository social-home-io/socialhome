"""The newest sync page a household holds per space (migration 0151).

``docs/protocol/sync-pages.md``: one row per space, the same on the signer
and on a member — the signed header and its bucket vector. Replaced only by
a newer page, deleted on an authority rotation (trigger) and with the
space.
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

    async def put(self, page: StoredSyncPage, *, key: str) -> bool:
        """Store *page* — verified (or signed) under the space public key
        *key* (hex) — as the space's page, only while the space still pins
        that key and the page's epoch, and only over an older page by
        ``(epoch, issued_at, seq)``. Whether it was stored."""
        ...


class SqliteSpaceSyncPageRepo:
    """SQLite-backed :class:`AbstractSpaceSyncPageRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def get(self, space_id: str) -> StoredSyncPage | None:
        row = await self._db.fetchone(
            "SELECT page_json, vector FROM space_sync_pages WHERE space_id=?",
            (space_id,),
        )
        if row is None:
            return None
        return StoredSyncPage(
            space_id=space_id,
            page_json=str(row["page_json"]),
            vector=bytes(row["vector"]),
        )

    async def put(self, page: StoredSyncPage, *, key: str) -> bool:
        # One statement, so nothing slips in between the checks and the
        # write (final review minor 6): the space must still pin the key and
        # epoch the page was verified against (a space this household no
        # longer holds has no row — nothing is stored), and a held page is
        # replaced only by a newer one.
        return (
            await self._db.enqueue_rowcount(
                "INSERT INTO space_sync_pages(space_id, page_json, vector)"
                " SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM spaces WHERE id=?"
                " AND lower(identity_public_key)=lower(?)"
                " AND COALESCE(authority_key_epoch, 0)=json_extract(?, '$.epoch'))"
                " ON CONFLICT(space_id) DO UPDATE SET"
                " page_json=excluded.page_json, vector=excluded.vector"
                " WHERE (json_extract(excluded.page_json, '$.epoch'),"
                " json_extract(excluded.page_json, '$.issued_at'),"
                " json_extract(excluded.page_json, '$.seq'))"
                " > (json_extract(space_sync_pages.page_json, '$.epoch'),"
                " json_extract(space_sync_pages.page_json, '$.issued_at'),"
                " json_extract(space_sync_pages.page_json, '$.seq'))",
                (
                    page.space_id,
                    page.page_json,
                    page.vector,
                    page.space_id,
                    key,
                    page.page_json,
                ),
            )
            > 0
        )


__all__ = ["AbstractSpaceSyncPageRepo", "SqliteSpaceSyncPageRepo"]
