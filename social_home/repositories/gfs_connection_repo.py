"""GFS connection repository — wraps gfs_connections + gfs_space_publications."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.federation import GfsConnection, GfsSpacePublication
from .base import rows_to_dicts


@runtime_checkable
class AbstractGfsConnectionRepo(Protocol):
    """Interface for GFS connection persistence."""

    async def save(self, conn: GfsConnection) -> None: ...
    async def get(self, gfs_id: str) -> GfsConnection | None: ...
    async def list_active(self) -> list[GfsConnection]: ...
    async def update_status(self, gfs_id: str, status: str) -> None: ...
    async def delete(self, gfs_id: str) -> None: ...
    async def publish_space(self, space_id: str, gfs_id: str) -> None: ...
    async def unpublish_space(self, space_id: str, gfs_id: str) -> None: ...
    async def list_publications(self, gfs_id: str) -> list[GfsSpacePublication]: ...
    async def list_gfs_for_space(self, space_id: str) -> list[GfsConnection]: ...
    async def count_published_spaces(self, gfs_id: str) -> int: ...
    async def list_publications_all(self) -> list[dict]: ...


class SqliteGfsConnectionRepo:
    """SQLite-backed :class:`AbstractGfsConnectionRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def save(self, conn: GfsConnection) -> None:
        await self._db.enqueue(
            """
            INSERT INTO gfs_connections(
                id, gfs_instance_id, display_name, public_key,
                endpoint_url, status, paired_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                gfs_instance_id=excluded.gfs_instance_id,
                display_name=excluded.display_name,
                public_key=excluded.public_key,
                endpoint_url=excluded.endpoint_url,
                status=excluded.status,
                paired_at=excluded.paired_at
            """,
            (
                conn.id,
                conn.gfs_instance_id,
                conn.display_name,
                conn.public_key,
                conn.endpoint_url,
                conn.status,
                conn.paired_at,
            ),
        )

    async def get(self, gfs_id: str) -> GfsConnection | None:
        row = await self._db.fetchone(
            "SELECT * FROM gfs_connections WHERE id=?",
            (gfs_id,),
        )
        if row is None:
            return None
        return _row_to_conn(dict(zip(row.keys(), tuple(row))))

    async def list_active(self) -> list[GfsConnection]:
        rows = await self._db.fetchall(
            "SELECT * FROM gfs_connections WHERE status='active'"
            " ORDER BY paired_at DESC",
        )
        return [_row_to_conn(r) for r in rows_to_dicts(rows)]

    async def update_status(self, gfs_id: str, status: str) -> None:
        await self._db.enqueue(
            "UPDATE gfs_connections SET status=? WHERE id=?",
            (status, gfs_id),
        )

    async def delete(self, gfs_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM gfs_space_publications WHERE gfs_connection_id=?",
            (gfs_id,),
        )
        await self._db.enqueue(
            "DELETE FROM gfs_connections WHERE id=?",
            (gfs_id,),
        )

    async def publish_space(self, space_id: str, gfs_id: str) -> None:
        await self._db.enqueue(
            "INSERT OR IGNORE INTO gfs_space_publications(space_id, gfs_connection_id)"
            " VALUES(?, ?)",
            (space_id, gfs_id),
        )

    async def unpublish_space(self, space_id: str, gfs_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM gfs_space_publications"
            " WHERE space_id=? AND gfs_connection_id=?",
            (space_id, gfs_id),
        )

    async def list_publications(self, gfs_id: str) -> list[GfsSpacePublication]:
        rows = await self._db.fetchall(
            "SELECT * FROM gfs_space_publications WHERE gfs_connection_id=?"
            " ORDER BY published_at DESC",
            (gfs_id,),
        )
        return [
            GfsSpacePublication(
                space_id=r["space_id"],
                gfs_connection_id=r["gfs_connection_id"],
                published_at=r["published_at"],
            )
            for r in rows_to_dicts(rows)
        ]

    async def list_gfs_for_space(self, space_id: str) -> list[GfsConnection]:
        rows = await self._db.fetchall(
            "SELECT gc.* FROM gfs_connections gc"
            " INNER JOIN gfs_space_publications gsp"
            "   ON gc.id = gsp.gfs_connection_id"
            " WHERE gsp.space_id=?"
            " ORDER BY gc.paired_at DESC",
            (space_id,),
        )
        return [_row_to_conn(r) for r in rows_to_dicts(rows)]

    async def count_published_spaces(self, gfs_id: str) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM gfs_space_publications"
            " WHERE gfs_connection_id=?",
            (gfs_id,),
        )
        return int(row["n"]) if row else 0

    async def list_publications_all(self) -> list[dict]:
        """Aggregate every (space, gfs) publication with join-friendly
        metadata. Used by the admin Spaces tab to render the "currently
        published to" list without N+1 per-space fetches.
        """
        rows = await self._db.fetchall(
            """
            SELECT gsp.space_id            AS space_id,
                   gsp.gfs_connection_id   AS gfs_id,
                   gsp.published_at        AS published_at,
                   gc.display_name         AS gfs_display_name,
                   gc.endpoint_url         AS gfs_endpoint_url,
                   s.name                  AS space_name,
                   s.emoji                 AS space_emoji
              FROM gfs_space_publications AS gsp
              JOIN gfs_connections AS gc ON gc.id = gsp.gfs_connection_id
              LEFT JOIN spaces AS s ON s.id = gsp.space_id
             ORDER BY gsp.published_at DESC
            """,
        )
        return rows_to_dicts(rows)


def _row_to_conn(r: dict) -> GfsConnection:
    return GfsConnection(
        id=r["id"],
        gfs_instance_id=r["gfs_instance_id"],
        display_name=r["display_name"],
        public_key=r["public_key"],
        endpoint_url=r["endpoint_url"],
        status=r["status"],
        paired_at=r["paired_at"],
        created_at=r.get("created_at"),
    )
