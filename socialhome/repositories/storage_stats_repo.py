"""Storage stats repository — read-only aggregation for §5.2 quota.

Wraps the SQL surface used by :class:`StorageQuotaService` so the
service depends only on the abstract protocol — never on raw SQL.

Tables touched:

* ``feed_posts.file_meta_json`` — household feed attachments
* ``space_posts.file_meta_json`` — space attachments
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase


@runtime_checkable
class AbstractStorageStatsRepo(Protocol):
    async def list_file_meta_blobs(self) -> list[str]: ...


class SqliteStorageStatsRepo:
    """SQLite-backed :class:`AbstractStorageStatsRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def list_file_meta_blobs(self) -> list[str]:
        """Return every non-NULL file_meta_json blob across post tables."""
        out: list[str] = []
        rows = await self._db.fetchall(
            "SELECT file_meta_json FROM feed_posts WHERE file_meta_json IS NOT NULL",
        )
        for r in rows:
            v = r["file_meta_json"]
            if v:
                out.append(v)
        rows = await self._db.fetchall(
            "SELECT file_meta_json FROM space_posts WHERE file_meta_json IS NOT NULL",
        )
        for r in rows:
            v = r["file_meta_json"]
            if v:
                out.append(v)
        return out
