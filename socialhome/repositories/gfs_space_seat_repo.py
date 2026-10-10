"""GFS subscriber-seat repository — wraps ``gfs_space_seats`` (0092).

A row says "the connection server ``gfs_instance_id`` holds a subscriber
seat of this household for ``space_id``": the only servers an
identity-bound (un)subscribe for that space may ever be sent to. It also
binds the seat to the local connection, server key and URL it was taken
over, so an id-claiming impostor is never mistaken for the seat's server
and the v_44 pin-heal anchor can follow a genuine re-pair.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.gfs_space_seat import GfsSpaceSeat

# Re-exported for repo-level imports.
__all__ = ["AbstractGfsSpaceSeatRepo", "GfsSpaceSeat", "SqliteGfsSpaceSeatRepo"]


@runtime_checkable
class AbstractGfsSpaceSeatRepo(Protocol):
    async def record(self, seat: GfsSpaceSeat) -> None: ...
    async def forget(self, space_id: str, gfs_instance_id: str) -> None: ...
    async def get(self, space_id: str, gfs_instance_id: str) -> GfsSpaceSeat | None: ...
    async def list_for_space(self, space_id: str) -> list[GfsSpaceSeat]: ...
    async def list_for_gfs(self, gfs_instance_id: str) -> list[GfsSpaceSeat]: ...
    async def list_all(self) -> list[GfsSpaceSeat]: ...
    async def mark_detached(
        self, space_id: str, gfs_instance_id: str, *, at: str | None
    ) -> None: ...
    async def mark_released(self, space_id: str, gfs_instance_id: str) -> None: ...
    async def set_detached_at(
        self, space_id: str, gfs_instance_id: str, at: str
    ) -> None: ...
    async def set_expiry_seen(
        self, space_id: str, gfs_instance_id: str, at: str | None
    ) -> None: ...
    async def mark_refollow_warned(
        self, space_id: str, gfs_instance_id: str
    ) -> None: ...


class SqliteGfsSpaceSeatRepo:
    """SQLite-backed :class:`AbstractGfsSpaceSeatRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def record(self, seat: GfsSpaceSeat) -> None:
        """Upsert: re-taking a seat keeps its first ``seated_at`` and binds
        it to the connection, key and URL it was taken over now."""
        await self._db.enqueue(
            "INSERT INTO gfs_space_seats("
            " space_id, gfs_instance_id, gfs_connection_id, gfs_public_key,"
            " gfs_inbox_url"
            ") VALUES(?, ?, ?, ?, ?)"
            " ON CONFLICT(space_id, gfs_instance_id) DO UPDATE SET"
            " gfs_connection_id=excluded.gfs_connection_id,"
            " gfs_public_key=excluded.gfs_public_key,"
            " gfs_inbox_url=excluded.gfs_inbox_url,"
            " detached=0, detached_at=NULL, released=0, expiry_seen_at=NULL,"
            " refollow_warned=0",
            (
                seat.space_id,
                seat.gfs_instance_id,
                seat.gfs_connection_id,
                seat.gfs_public_key,
                seat.gfs_inbox_url,
            ),
        )

    async def forget(self, space_id: str, gfs_instance_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM gfs_space_seats WHERE space_id=? AND gfs_instance_id=?",
            (space_id, gfs_instance_id),
        )

    async def get(self, space_id: str, gfs_instance_id: str) -> GfsSpaceSeat | None:
        row = await self._db.fetchone(
            "SELECT * FROM gfs_space_seats WHERE space_id=? AND gfs_instance_id=?",
            (space_id, gfs_instance_id),
        )
        return None if row is None else _seat(row)

    async def list_for_space(self, space_id: str) -> list[GfsSpaceSeat]:
        """Every server's seat for *space_id*."""
        rows = await self._db.fetchall(
            "SELECT * FROM gfs_space_seats WHERE space_id=? ORDER BY gfs_instance_id",
            (space_id,),
        )
        return [_seat(r) for r in rows]

    async def list_for_gfs(self, gfs_instance_id: str) -> list[GfsSpaceSeat]:
        """Every seat recorded under server id *gfs_instance_id* — the
        caller still checks the binding against the connection."""
        rows = await self._db.fetchall(
            "SELECT * FROM gfs_space_seats WHERE gfs_instance_id=? ORDER BY space_id",
            (gfs_instance_id,),
        )
        return [_seat(r) for r in rows]

    async def list_all(self) -> list[GfsSpaceSeat]:
        rows = await self._db.fetchall(
            "SELECT * FROM gfs_space_seats ORDER BY space_id, gfs_instance_id"
        )
        return [_seat(r) for r in rows]

    async def mark_detached(
        self, space_id: str, gfs_instance_id: str, *, at: str | None
    ) -> None:
        """The server was unpaired: keep the row, unreleased, stamped
        *at* (``None`` while the clock can't be trusted)."""
        await self._db.enqueue(
            "UPDATE gfs_space_seats SET detached=1, released=0,"
            " detached_at=COALESCE(detached_at, ?), expiry_seen_at=NULL"
            " WHERE space_id=? AND gfs_instance_id=?",
            (at, space_id, gfs_instance_id),
        )

    async def mark_released(self, space_id: str, gfs_instance_id: str) -> None:
        await self._db.enqueue(
            "UPDATE gfs_space_seats SET released=1"
            " WHERE space_id=? AND gfs_instance_id=? AND detached=1",
            (space_id, gfs_instance_id),
        )

    async def set_detached_at(
        self, space_id: str, gfs_instance_id: str, at: str
    ) -> None:
        await self._db.enqueue(
            "UPDATE gfs_space_seats SET detached_at=?, expiry_seen_at=NULL"
            " WHERE space_id=? AND gfs_instance_id=?",
            (at, space_id, gfs_instance_id),
        )

    async def set_expiry_seen(
        self, space_id: str, gfs_instance_id: str, at: str | None
    ) -> None:
        await self._db.enqueue(
            "UPDATE gfs_space_seats SET expiry_seen_at=?"
            " WHERE space_id=? AND gfs_instance_id=?",
            (at, space_id, gfs_instance_id),
        )

    async def mark_refollow_warned(self, space_id: str, gfs_instance_id: str) -> None:
        await self._db.enqueue(
            "UPDATE gfs_space_seats SET refollow_warned=1"
            " WHERE space_id=? AND gfs_instance_id=?",
            (space_id, gfs_instance_id),
        )


def _seat(row: Any) -> GfsSpaceSeat:
    return GfsSpaceSeat(
        space_id=row["space_id"],
        gfs_instance_id=row["gfs_instance_id"],
        gfs_connection_id=row["gfs_connection_id"],
        gfs_public_key=row["gfs_public_key"],
        gfs_inbox_url=row["gfs_inbox_url"],
        detached=bool(row["detached"]),
        detached_at=row["detached_at"],
        released=bool(row["released"]),
        expiry_seen_at=row["expiry_seen_at"],
        refollow_warned=bool(row["refollow_warned"]),
    )
