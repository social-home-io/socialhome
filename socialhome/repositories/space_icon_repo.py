"""Space icon (avatar) blob storage — distinct from the cover banner.

Mirrors :class:`SqliteSpaceCoverRepo`: one WebP blob per space, keyed by
``space_id``. The parent ``spaces.icon_hash`` column mirrors the blob hash
so the frontend can cache-bust via ``/api/spaces/{id}/icon?v=<hash>``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase


@runtime_checkable
class AbstractSpaceIconRepo(Protocol):
    async def get(self, space_id: str) -> tuple[bytes, str] | None: ...
    async def set(
        self,
        space_id: str,
        *,
        bytes_webp: bytes,
        hash: str,
        width: int,
        height: int,
    ) -> None: ...
    async def clear(self, space_id: str) -> None: ...
    async def replace_bytes_if_hash(
        self, space_id: str, *, bytes_webp: bytes, hash: str
    ) -> bool: ...


class SqliteSpaceIconRepo:
    """SQLite implementation of :class:`AbstractSpaceIconRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def get(self, space_id: str) -> tuple[bytes, str] | None:
        row = await self._db.fetchone(
            "SELECT bytes_webp, hash FROM space_icons WHERE space_id=?",
            (space_id,),
        )
        if row is None:
            return None
        return bytes(row["bytes_webp"]), str(row["hash"])

    async def set(
        self,
        space_id: str,
        *,
        bytes_webp: bytes,
        hash: str,
        width: int,
        height: int,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO space_icons(
                space_id, bytes_webp, hash, width, height, updated_at
            ) VALUES(?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(space_id) DO UPDATE SET
                bytes_webp=excluded.bytes_webp,
                hash=excluded.hash,
                width=excluded.width,
                height=excluded.height,
                updated_at=excluded.updated_at
            """,
            (space_id, bytes_webp, hash, int(width), int(height)),
        )

    async def clear(self, space_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM space_icons WHERE space_id=?",
            (space_id,),
        )

    async def replace_bytes_if_hash(
        self, space_id: str, *, bytes_webp: bytes, hash: str
    ) -> bool:
        """Replace the stored bytes with *bytes_webp* (a rendition of them)
        only while the stored version is still *hash* — a set or clear in
        between is never undone. ``True`` when it was replaced."""
        changed = await self._db.enqueue_rowcount(
            "UPDATE space_icons SET bytes_webp=?, updated_at=datetime('now') "
            "WHERE space_id=? AND hash=?",
            (bytes_webp, space_id, hash),
        )
        return changed > 0
