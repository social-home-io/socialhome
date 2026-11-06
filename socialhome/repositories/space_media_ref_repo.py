"""``space_media_refs`` — where a space file lives in a GFS media cache.

One row per local filename (migration 0142): the blob address, suite and
epoch a ``media_refs`` descriptor named for it. Read by the media route's
fetch-on-view and by the prefetch on arrival
(:mod:`socialhome.services.space_media_gfs_fetcher`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.space_media_ref import SpaceMediaRef


@runtime_checkable
class AbstractSpaceMediaRefRepo(Protocol):
    async def save_many(self, refs: Sequence[SpaceMediaRef]) -> None: ...

    async def get(self, filename: str) -> SpaceMediaRef | None: ...


class SqliteSpaceMediaRefRepo:
    """SQLite-backed :class:`AbstractSpaceMediaRefRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def save_many(self, refs: Sequence[SpaceMediaRef]) -> None:
        """Keep each descriptor entry, write-once: a file already described
        (by any item, of any space) is never re-pointed, and an entry for a
        space this household does not hold is dropped. The caller refuses a
        conflicting descriptor before it gets here (and logs it)."""
        for ref in refs:
            await self._db.enqueue(
                """
                INSERT INTO space_media_refs(
                    filename, space_id, blob_id, suite, epoch, size, scope,
                    addr_suite, item_id, origin)
                SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                WHERE EXISTS (SELECT 1 FROM spaces WHERE id = ?)
                ON CONFLICT(filename) DO NOTHING
                """,
                (
                    ref.filename,
                    ref.space_id,
                    ref.blob_id,
                    ref.suite,
                    ref.epoch,
                    ref.size,
                    ref.scope,
                    ref.addr_suite,
                    ref.item_id,
                    ref.origin,
                    ref.space_id,
                ),
            )

    async def get(self, filename: str) -> SpaceMediaRef | None:
        row = await self._db.fetchone(
            "SELECT filename, space_id, blob_id, suite, epoch, size, scope,"
            " addr_suite, item_id, origin, created_at FROM space_media_refs"
            " WHERE filename = ?",
            (filename,),
        )
        if row is None:
            return None
        return SpaceMediaRef(
            filename=row["filename"],
            space_id=row["space_id"],
            blob_id=row["blob_id"],
            suite=row["suite"],
            epoch=int(row["epoch"]),
            size=int(row["size"]),
            scope=row["scope"],
            addr_suite=row["addr_suite"],
            item_id=row["item_id"],
            origin=row["origin"],
            created_at=row["created_at"],
        )
