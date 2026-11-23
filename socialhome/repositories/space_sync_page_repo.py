"""The newest sync page a household holds per space (migration 0151).

``docs/protocol/sync-pages.md``: one row per (space, signer), the same on a
signer and on a member — the signed header and its bucket vector. Replaced
only by a newer page of that signer, deleted on an authority rotation
(trigger) and with the space.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.space_sync_page import StoredSyncPage


@runtime_checkable
class AbstractSpaceSyncPageRepo(Protocol):
    async def get(self, space_id: str) -> StoredSyncPage | None:
        """The newest page held for the space across its signers (by
        ``(epoch, issued_at, seq)``), or ``None``."""
        ...

    async def list_all(self, space_id: str) -> list[StoredSyncPage]:
        """The newest page held per signer of the space, newest first."""
        ...

    async def put(self, page: StoredSyncPage, *, key: str) -> bool:
        """Store *page* — verified (or signed) under the space public key
        *key* (hex) — as its SIGNER's page of the space, only while the
        space still pins that key and the page's epoch, and only over an
        older page of that signer by ``(epoch, issued_at, seq)``. Whether
        it was stored."""
        ...


class SqliteSpaceSyncPageRepo:
    """SQLite-backed :class:`AbstractSpaceSyncPageRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def get(self, space_id: str) -> StoredSyncPage | None:
        rows = await self.list_all(space_id)
        return rows[0] if rows else None

    async def list_all(self, space_id: str) -> list[StoredSyncPage]:
        rows = await self._db.fetchall(
            "SELECT page_json, vector FROM space_sync_pages WHERE space_id=?"
            " ORDER BY json_extract(page_json, '$.epoch') DESC,"
            " json_extract(page_json, '$.issued_at') DESC,"
            " json_extract(page_json, '$.seq') DESC",
            (space_id,),
        )
        return [
            StoredSyncPage(
                space_id=space_id,
                page_json=str(row["page_json"]),
                vector=bytes(row["vector"]),
            )
            for row in rows
        ]

    async def put(self, page: StoredSyncPage, *, key: str) -> bool:
        # One statement, so nothing slips in between the checks and the
        # write (final review minor 6): the space must still pin the key and
        # epoch the page was verified against (a space this household no
        # longer holds has no row — nothing is stored), and a held page is
        # replaced only by a newer one.
        return (
            await self._db.enqueue_rowcount(
                "INSERT INTO space_sync_pages(space_id, signer, page_json, vector)"
                " SELECT ?, json_extract(?, '$.signer'), ?, ?"
                " WHERE json_extract(?, '$.signer') IS NOT NULL"
                " AND EXISTS (SELECT 1 FROM spaces WHERE id=?"
                " AND lower(identity_public_key)=lower(?)"
                " AND COALESCE(authority_key_epoch, 0)=json_extract(?, '$.epoch'))"
                " ON CONFLICT(space_id, signer) DO UPDATE SET"
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
                    page.page_json,
                    page.vector,
                    page.page_json,
                    page.space_id,
                    key,
                    page.page_json,
                ),
            )
            > 0
        )


__all__ = ["AbstractSpaceSyncPageRepo", "SqliteSpaceSyncPageRepo"]
