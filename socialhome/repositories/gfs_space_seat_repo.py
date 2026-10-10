"""GFS subscriber-seat repository — wraps ``gfs_space_seats`` (0092).

A row says "the connection server ``gfs_instance_id`` holds a subscriber
seat of this household for ``space_id``": the only servers an
identity-bound (un)subscribe for that space may ever be sent to. It also
remembers the local connection the seat was taken over and the server key
pinned on it, so the v_44 pin-heal anchor can follow a same-key re-pair.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase


@runtime_checkable
class AbstractGfsSpaceSeatRepo(Protocol):
    async def record(
        self,
        space_id: str,
        gfs_instance_id: str,
        *,
        gfs_connection_id: str,
        gfs_public_key: str,
    ) -> None: ...
    async def forget(self, space_id: str, gfs_instance_id: str) -> None: ...
    async def get_binding(
        self, space_id: str, gfs_instance_id: str
    ) -> tuple[str | None, str | None] | None: ...
    async def list_for_space(self, space_id: str) -> list[str]: ...
    async def list_for_gfs(self, gfs_instance_id: str) -> list[str]: ...


class SqliteGfsSpaceSeatRepo:
    """SQLite-backed :class:`AbstractGfsSpaceSeatRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def record(
        self,
        space_id: str,
        gfs_instance_id: str,
        *,
        gfs_connection_id: str,
        gfs_public_key: str,
    ) -> None:
        """Upsert: re-taking a seat keeps its first ``seated_at`` and binds
        it to the connection (and pinned key) it was taken over now."""
        await self._db.enqueue(
            "INSERT INTO gfs_space_seats("
            " space_id, gfs_instance_id, gfs_connection_id, gfs_public_key"
            ") VALUES(?, ?, ?, ?)"
            " ON CONFLICT(space_id, gfs_instance_id) DO UPDATE SET"
            " gfs_connection_id=excluded.gfs_connection_id,"
            " gfs_public_key=excluded.gfs_public_key",
            (space_id, gfs_instance_id, gfs_connection_id, gfs_public_key),
        )

    async def forget(self, space_id: str, gfs_instance_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM gfs_space_seats WHERE space_id=? AND gfs_instance_id=?",
            (space_id, gfs_instance_id),
        )

    async def get_binding(
        self, space_id: str, gfs_instance_id: str
    ) -> tuple[str | None, str | None] | None:
        """``(gfs_connection_id, gfs_public_key)`` of the seat, or ``None``
        when no such seat is recorded."""
        row = await self._db.fetchone(
            "SELECT gfs_connection_id, gfs_public_key FROM gfs_space_seats"
            " WHERE space_id=? AND gfs_instance_id=?",
            (space_id, gfs_instance_id),
        )
        if row is None:
            return None
        return row["gfs_connection_id"], row["gfs_public_key"]

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
