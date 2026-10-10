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
from ..domain.gfs_space_seat import UNKNOWN_GFS_SERVER, GfsSpaceSeat

# Re-exported for repo-level imports.
__all__ = ["AbstractGfsSpaceSeatRepo", "GfsSpaceSeat", "SqliteGfsSpaceSeatRepo"]


@runtime_checkable
class AbstractGfsSpaceSeatRepo(Protocol):
    async def record(self, seat: GfsSpaceSeat) -> None: ...
    async def forget(self, space_id: str, gfs_instance_id: str) -> None: ...
    async def get(self, space_id: str, gfs_instance_id: str) -> GfsSpaceSeat | None: ...
    async def list_for_space(self, space_id: str) -> list[GfsSpaceSeat]: ...
    async def list_for_gfs(self, gfs_instance_id: str) -> list[GfsSpaceSeat]: ...
    async def mark_legacy_release(self, space_id: str, authority_pk: str) -> None: ...
    async def get_legacy_release(
        self, space_id: str, *, max_age_days: int
    ) -> str | None: ...
    async def clear_legacy_release(self, space_id: str) -> None: ...
    async def purge_legacy_releases(self, *, max_age_days: int) -> int: ...


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
            " gfs_inbox_url=excluded.gfs_inbox_url",
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
        if gfs_instance_id == UNKNOWN_GFS_SERVER:
            return None
        row = await self._db.fetchone(
            "SELECT * FROM gfs_space_seats WHERE space_id=? AND gfs_instance_id=?",
            (space_id, gfs_instance_id),
        )
        return None if row is None else _seat(row)

    async def list_for_space(self, space_id: str) -> list[GfsSpaceSeat]:
        """Every server's seat for *space_id*."""
        rows = await self._db.fetchall(
            "SELECT * FROM gfs_space_seats WHERE space_id=? AND gfs_instance_id<>?"
            " ORDER BY gfs_instance_id",
            (space_id, UNKNOWN_GFS_SERVER),
        )
        return [_seat(r) for r in rows]

    async def list_for_gfs(self, gfs_instance_id: str) -> list[GfsSpaceSeat]:
        """Every seat recorded under server id *gfs_instance_id* — the
        caller still checks the binding against the connection."""
        if gfs_instance_id == UNKNOWN_GFS_SERVER:
            return []
        rows = await self._db.fetchall(
            "SELECT * FROM gfs_space_seats WHERE gfs_instance_id=? ORDER BY space_id",
            (gfs_instance_id,),
        )
        return [_seat(r) for r in rows]

    # ── Pending legacy release (a seat at a server we can't name) ─────────

    async def mark_legacy_release(self, space_id: str, authority_pk: str) -> None:
        """Remember that a pre-v44 follower seat of *space_id* is still held
        somewhere unknown; *authority_pk* (the space's pinned key) is what a
        relay frame must verify against to prove its server seats us."""
        await self._db.enqueue(
            "INSERT INTO gfs_space_seats(space_id, gfs_instance_id,"
            " space_authority_pk) VALUES(?, ?, ?)"
            " ON CONFLICT(space_id, gfs_instance_id) DO UPDATE SET"
            " space_authority_pk=excluded.space_authority_pk,"
            " seated_at=datetime('now')",
            (space_id, UNKNOWN_GFS_SERVER, authority_pk),
        )

    async def get_legacy_release(
        self, space_id: str, *, max_age_days: int
    ) -> str | None:
        """The pinned authority key of a pending legacy release younger
        than *max_age_days*, or ``None``."""
        row = await self._db.fetchone(
            "SELECT space_authority_pk FROM gfs_space_seats"
            " WHERE space_id=? AND gfs_instance_id=?"
            " AND seated_at >= datetime('now', ?)",
            (space_id, UNKNOWN_GFS_SERVER, f"-{int(max_age_days)} days"),
        )
        return None if row is None else row["space_authority_pk"]

    async def clear_legacy_release(self, space_id: str) -> None:
        await self.forget(space_id, UNKNOWN_GFS_SERVER)

    async def purge_legacy_releases(self, *, max_age_days: int) -> int:
        """Drop pending legacy releases older than *max_age_days*."""
        return await self._db.enqueue_rowcount(
            "DELETE FROM gfs_space_seats WHERE gfs_instance_id=?"
            " AND seated_at < datetime('now', ?)",
            (UNKNOWN_GFS_SERVER, f"-{int(max_age_days)} days"),
        )


def _seat(row: Any) -> GfsSpaceSeat:
    return GfsSpaceSeat(
        space_id=row["space_id"],
        gfs_instance_id=row["gfs_instance_id"],
        gfs_connection_id=row["gfs_connection_id"],
        gfs_public_key=row["gfs_public_key"],
        gfs_inbox_url=row["gfs_inbox_url"],
    )
