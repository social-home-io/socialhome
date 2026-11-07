"""``space_media_refs`` — where a space file lives in a GFS media cache.

One row per local name (migration 0142 — the reader's own ``sg-`` name for
a file a ``media_refs`` descriptor named): the blob address, suite and epoch,
and the item and delivering household it is bound to. Read by the media route's
fetch-on-view and by the prefetch on arrival
(:mod:`socialhome.services.space_media_gfs_fetcher`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.space_media_ref import SpaceMediaRef


@runtime_checkable
class AbstractSpaceMediaRefRepo(Protocol):
    async def save_many(self, refs: Sequence[SpaceMediaRef]) -> None: ...

    async def get(self, local_name: str) -> SpaceMediaRef | None: ...

    async def list_for_item(
        self, space_id: str, item_id: str
    ) -> list[SpaceMediaRef]: ...


class SqliteSpaceMediaRefRepo:
    """SQLite-backed :class:`AbstractSpaceMediaRefRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def save_many(self, refs: Sequence[SpaceMediaRef]) -> None:
        """Keep each descriptor entry. A local name already held is never
        re-pointed — except by the SAME item to a newer epoch (its file
        sealed again after a key rotation). An entry for a space this
        household does not hold is dropped."""
        for ref in refs:
            await self._db.enqueue(
                """
                INSERT INTO space_media_refs(
                    local_name, filename, space_id, blob_id, suite, epoch,
                    size, scope, addr_suite, item_id, origin)
                SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                WHERE EXISTS (SELECT 1 FROM spaces WHERE id = ?)
                ON CONFLICT(local_name) DO UPDATE SET
                    blob_id = excluded.blob_id,
                    suite = excluded.suite,
                    epoch = excluded.epoch,
                    size = excluded.size,
                    scope = excluded.scope,
                    addr_suite = excluded.addr_suite
                WHERE space_media_refs.item_id = excluded.item_id
                  AND space_media_refs.space_id = excluded.space_id
                  AND excluded.epoch > space_media_refs.epoch
                """,
                (
                    ref.local_name,
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

    async def get(self, local_name: str) -> SpaceMediaRef | None:
        row = await self._db.fetchone(
            f"SELECT {_COLS} FROM space_media_refs WHERE local_name = ?",
            (local_name,),
        )
        return _row(row) if row is not None else None

    async def list_for_item(self, space_id: str, item_id: str) -> list[SpaceMediaRef]:
        """The rows of item *item_id* of *space_id*, oldest first."""
        rows = await self._db.fetchall(
            f"SELECT {_COLS} FROM space_media_refs"
            " WHERE space_id = ? AND item_id = ? ORDER BY rowid",
            (space_id, item_id),
        )
        return [_row(r) for r in rows]


_COLS = (
    "local_name, filename, space_id, blob_id, suite, epoch, size, scope,"
    " addr_suite, item_id, origin, created_at"
)


def _row(row: Any) -> SpaceMediaRef:
    return SpaceMediaRef(
        local_name=row["local_name"],
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
