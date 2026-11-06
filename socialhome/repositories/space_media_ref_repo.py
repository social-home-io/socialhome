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

    async def delete(self, filename: str) -> None: ...


class SqliteSpaceMediaRefRepo:
    """SQLite-backed :class:`AbstractSpaceMediaRefRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def save_many(self, refs: Sequence[SpaceMediaRef]) -> None:
        """Keep each descriptor entry. A newer entry for a file of the SAME
        space replaces the old one (a re-sealed blob, a later epoch); a file
        another space already described is never re-pointed, and an entry
        for a space this household does not hold is dropped."""
        for ref in refs:
            await self._db.enqueue(
                """
                INSERT INTO space_media_refs(
                    filename, space_id, blob_id, suite, epoch, size)
                SELECT ?, ?, ?, ?, ?, ?
                WHERE EXISTS (SELECT 1 FROM spaces WHERE id = ?)
                ON CONFLICT(filename) DO UPDATE SET
                    blob_id = excluded.blob_id,
                    suite = excluded.suite,
                    epoch = excluded.epoch,
                    size = excluded.size
                WHERE space_media_refs.space_id = excluded.space_id
                """,
                (
                    ref.filename,
                    ref.space_id,
                    ref.blob_id,
                    ref.suite,
                    ref.epoch,
                    ref.size,
                    ref.space_id,
                ),
            )

    async def get(self, filename: str) -> SpaceMediaRef | None:
        row = await self._db.fetchone(
            "SELECT filename, space_id, blob_id, suite, epoch, size, created_at"
            " FROM space_media_refs WHERE filename = ?",
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
            created_at=row["created_at"],
        )

    async def delete(self, filename: str) -> None:
        await self._db.enqueue(
            "DELETE FROM space_media_refs WHERE filename = ?", (filename,)
        )
