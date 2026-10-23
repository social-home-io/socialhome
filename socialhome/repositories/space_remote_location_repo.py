"""Storage for cross-household location pins on a space map.

When a remote household ships a ``SPACE_LOCATION_UPDATED`` event for
one of their users who's a member of a space we host, the receiver's
:class:`PrivateSpaceInviteHandler._on_space_location_updated` calls
``upsert`` here. ``SpacePresenceView`` then merges these rows into
the ``/api/spaces/{id}/presence`` response so the rendered map
shows the remote member's pin alongside the local members'.

See migration ``0010_space_remote_member_locations.sql`` for the
schema audit and shape rationale.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..db.database import AsyncDatabase
from .base import rows_to_dicts


@dataclass(slots=True, frozen=True)
class SpaceRemoteLocation:
    """A single space-scoped location pin from a remote member.

    Mirrors the SPACE_LOCATION_UPDATED payload shape so the inbound
    handler can hand a payload to ``upsert`` without juggling field
    names. ``mode`` distinguishes the two privacy tiers — ``gps``
    carries lat/lon, ``zone_only`` carries zone_id/zone_name and
    leaves coordinates None.
    """

    space_id: str
    instance_id: str
    user_id: str
    mode: str
    latitude: float | None = None
    longitude: float | None = None
    accuracy_m: float | None = None
    zone_id: str | None = None
    zone_name: str | None = None
    updated_at: str | None = None


@runtime_checkable
class AbstractSpaceRemoteLocationRepo(Protocol):
    async def upsert(self, loc: SpaceRemoteLocation) -> None: ...

    async def list_for_space(self, space_id: str) -> list[SpaceRemoteLocation]: ...

    async def delete_for_member(
        self, space_id: str, instance_id: str, user_id: str
    ) -> None: ...


class SqliteSpaceRemoteLocationRepo:
    """SQLite-backed :class:`AbstractSpaceRemoteLocationRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def upsert(self, loc: SpaceRemoteLocation) -> None:
        await self._db.enqueue(
            """
            INSERT INTO space_remote_member_locations(
                space_id, instance_id, user_id, mode, latitude, longitude,
                accuracy_m, zone_id, zone_name, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?, COALESCE(?, datetime('now')))
            ON CONFLICT(space_id, instance_id, user_id) DO UPDATE SET
                mode=excluded.mode,
                latitude=excluded.latitude,
                longitude=excluded.longitude,
                accuracy_m=excluded.accuracy_m,
                zone_id=excluded.zone_id,
                zone_name=excluded.zone_name,
                updated_at=excluded.updated_at
            -- Newer wins: a pin that took a slower path (an outbox retry,
            -- the mesh) never overwrites a later one. Compared as instants,
            -- so a naive ``datetime('now')`` row and an ISO 8601 stamp
            -- order correctly; an unreadable stored stamp never blocks.
            WHERE julianday(space_remote_member_locations.updated_at) IS NULL
               OR julianday(excluded.updated_at)
                  >= julianday(space_remote_member_locations.updated_at)
            """,
            (
                loc.space_id,
                loc.instance_id,
                loc.user_id,
                loc.mode,
                loc.latitude,
                loc.longitude,
                loc.accuracy_m,
                loc.zone_id,
                loc.zone_name,
                loc.updated_at,
            ),
        )

    async def list_for_space(self, space_id: str) -> list[SpaceRemoteLocation]:
        rows = await self._db.fetchall(
            "SELECT * FROM space_remote_member_locations WHERE space_id=?",
            (space_id,),
        )
        return [_row(r) for r in rows_to_dicts(rows)]

    async def delete_for_member(
        self, space_id: str, instance_id: str, user_id: str
    ) -> None:
        await self._db.enqueue(
            "DELETE FROM space_remote_member_locations "
            "WHERE space_id=? AND instance_id=? AND user_id=?",
            (space_id, instance_id, user_id),
        )


def _row(row: dict) -> SpaceRemoteLocation:
    return SpaceRemoteLocation(
        space_id=row["space_id"],
        instance_id=row["instance_id"],
        user_id=row["user_id"],
        mode=row["mode"],
        latitude=row.get("latitude"),
        longitude=row.get("longitude"),
        accuracy_m=row.get("accuracy_m"),
        zone_id=row.get("zone_id"),
        zone_name=row.get("zone_name"),
        updated_at=row.get("updated_at"),
    )
