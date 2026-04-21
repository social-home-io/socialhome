"""Presence repository (§23.21).

Wraps the SQL surface used by :class:`PresenceService` so the service
depends only on the abstract protocol — never on raw SQL or the
SQLite implementation.

Tables touched:

* ``presence`` — local household-member presence + truncated coords.
* ``remote_presence`` — keyed on ``(from_instance, remote_username)``
  for federation peers.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.presence import PersonPresence


@runtime_checkable
class AbstractPresenceRepo(Protocol):
    async def list_active(self) -> list[PersonPresence]: ...
    async def get_by_username(self, username: str) -> PersonPresence | None: ...
    async def upsert_local(
        self,
        *,
        username: str,
        entity_id: str | None,
        state: str,
        zone_name: str | None,
        latitude: float | None,
        longitude: float | None,
        gps_accuracy_m: float | None,
        updated_at: str,
    ) -> None: ...
    async def upsert_remote(
        self,
        *,
        from_instance: str,
        remote_username: str,
        state: str,
        zone_name: str | None,
        latitude: float | None,
        longitude: float | None,
        gps_accuracy_m: float | None,
        updated_at: str,
    ) -> None: ...


def _to_presence(row) -> PersonPresence:
    picture_hash = row["picture_hash"]
    picture_url = (
        f"/api/users/{row['user_id']}/picture?v={picture_hash}"
        if picture_hash
        else None
    )
    return PersonPresence(
        username=row["username"],
        user_id=row["user_id"],
        display_name=row["display_name"],
        entity_id=row["entity_id"],
        state=row["state"],
        picture_url=picture_url,
        zone_name=row["zone_name"],
        latitude=row["latitude"],
        longitude=row["longitude"],
        gps_accuracy_m=row["gps_accuracy_m"],
    )


class SqlitePresenceRepo:
    """SQLite-backed :class:`AbstractPresenceRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def list_active(self) -> list[PersonPresence]:
        rows = await self._db.fetchall(
            """
            SELECT p.*, u.user_id, u.display_name, u.picture_hash
              FROM presence p
              JOIN users u ON u.username = p.username
             WHERE u.state = 'active'
             ORDER BY p.username
            """,
        )
        return [_to_presence(r) for r in rows]

    async def get_by_username(self, username: str) -> PersonPresence | None:
        row = await self._db.fetchone(
            """
            SELECT p.*, u.user_id, u.display_name, u.picture_hash
              FROM presence p
              JOIN users u ON u.username = p.username
             WHERE p.username = ?
            """,
            (username,),
        )
        return _to_presence(row) if row else None

    async def upsert_local(
        self,
        *,
        username: str,
        entity_id: str | None,
        state: str,
        zone_name: str | None,
        latitude: float | None,
        longitude: float | None,
        gps_accuracy_m: float | None,
        updated_at: str,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO presence(
                username, entity_id, state, zone_name,
                latitude, longitude, gps_accuracy_m, updated_at
            ) VALUES(?, COALESCE(?, ''), ?, ?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                state=excluded.state,
                zone_name=excluded.zone_name,
                latitude=excluded.latitude,
                longitude=excluded.longitude,
                gps_accuracy_m=excluded.gps_accuracy_m,
                updated_at=excluded.updated_at
            """,
            (
                username,
                entity_id,
                state,
                zone_name,
                latitude,
                longitude,
                gps_accuracy_m,
                updated_at,
            ),
        )

    async def upsert_remote(
        self,
        *,
        from_instance: str,
        remote_username: str,
        state: str,
        zone_name: str | None,
        latitude: float | None,
        longitude: float | None,
        gps_accuracy_m: float | None,
        updated_at: str,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO remote_presence(
                from_instance, remote_username, state, zone_name,
                latitude, longitude, gps_accuracy_m, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(from_instance, remote_username) DO UPDATE SET
                state=excluded.state,
                zone_name=excluded.zone_name,
                latitude=excluded.latitude,
                longitude=excluded.longitude,
                gps_accuracy_m=excluded.gps_accuracy_m,
                updated_at=excluded.updated_at
            """,
            (
                from_instance,
                remote_username,
                state,
                zone_name,
                latitude,
                longitude,
                gps_accuracy_m,
                updated_at,
            ),
        )
