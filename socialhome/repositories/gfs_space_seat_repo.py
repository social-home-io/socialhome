"""GFS subscriber-seat repository — wraps ``gfs_space_seats`` (0090).

A row says "the connection server ``gfs_instance_id`` holds a subscriber
seat of this household for ``space_id``": the only servers an
identity-bound (un)subscribe for that space may ever be sent to.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase


@runtime_checkable
class AbstractGfsSpaceSeatRepo(Protocol):
    async def record(self, space_id: str, gfs_instance_id: str) -> None: ...
    async def forget(self, space_id: str, gfs_instance_id: str) -> None: ...
    async def list_for_space(self, space_id: str) -> list[str]: ...
    async def list_for_gfs(self, gfs_instance_id: str) -> list[str]: ...


class SqliteGfsSpaceSeatRepo:
    """SQLite-backed :class:`AbstractGfsSpaceSeatRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def record(self, space_id: str, gfs_instance_id: str) -> None:
        """Idempotent: re-taking a seat keeps its first ``seated_at``."""
        await self._db.enqueue(
            "INSERT OR IGNORE INTO gfs_space_seats(space_id, gfs_instance_id)"
            " VALUES(?, ?)",
            (space_id, gfs_instance_id),
        )

    async def forget(self, space_id: str, gfs_instance_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM gfs_space_seats WHERE space_id=? AND gfs_instance_id=?",
            (space_id, gfs_instance_id),
        )

    async def list_for_space(self, space_id: str) -> list[str]:
        """The servers (``gfs_instance_id``) seating us for *space_id*."""
        rows = await self._db.fetchall(
            "SELECT gfs_instance_id FROM gfs_space_seats WHERE space_id=?"
            " ORDER BY gfs_instance_id",
            (space_id,),
        )
        return [r["gfs_instance_id"] for r in rows]

    async def list_for_gfs(self, gfs_instance_id: str) -> list[str]:
        """The spaces server *gfs_instance_id* seats us for."""
        rows = await self._db.fetchall(
            "SELECT space_id FROM gfs_space_seats WHERE gfs_instance_id=?"
            " ORDER BY space_id",
            (gfs_instance_id,),
        )
        return [r["space_id"] for r in rows]
