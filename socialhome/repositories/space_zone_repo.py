"""Space-zone repository — persistence for the per-space zone catalogue (§23.8.7).

Each space owns a small catalogue of named display circles. Members' GPS
positions are matched to zones client-side; the server never stores the
match or sends "member X is in zone Y" preprocessed labels.

The :class:`AbstractSpaceZoneRepo` protocol is the service-facing surface;
:class:`SqliteSpaceZoneRepo` implements it against the v1 schema. Mirrors
the style of :mod:`space_repo` — `enqueue` for writes, `fetchall` /
`fetchone` for reads, no business logic.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.space import SpaceZone


@runtime_checkable
class AbstractSpaceZoneRepo(Protocol):
    async def list_for_space(self, space_id: str) -> list[SpaceZone]: ...
    async def get(self, zone_id: str) -> SpaceZone | None: ...
    async def get_by_name(self, space_id: str, name: str) -> SpaceZone | None: ...
    async def count_for_space(self, space_id: str) -> int: ...
    async def upsert(self, zone: SpaceZone) -> None: ...
    async def delete(self, zone_id: str) -> None: ...


def _row_to_zone(row: dict | None) -> SpaceZone | None:
    if row is None:
        return None
    return SpaceZone(
        id=row["id"],
        space_id=row["space_id"],
        name=row["name"],
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
        radius_m=int(row["radius_m"]),
        color=row["color"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class SqliteSpaceZoneRepo:
    """SQLite-backed implementation of :class:`AbstractSpaceZoneRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def list_for_space(self, space_id: str) -> list[SpaceZone]:
        rows = await self._db.fetchall(
            "SELECT * FROM space_zones WHERE space_id=? ORDER BY name",
            (space_id,),
        )
        return [_row_to_zone(dict(r)) for r in rows]  # type: ignore[misc]

    async def get(self, zone_id: str) -> SpaceZone | None:
        row = await self._db.fetchone(
            "SELECT * FROM space_zones WHERE id=?",
            (zone_id,),
        )
        return _row_to_zone(dict(row) if row is not None else None)

    async def get_by_name(self, space_id: str, name: str) -> SpaceZone | None:
        row = await self._db.fetchone(
            "SELECT * FROM space_zones WHERE space_id=? AND name=?",
            (space_id, name),
        )
        return _row_to_zone(dict(row) if row is not None else None)

    async def count_for_space(self, space_id: str) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS c FROM space_zones WHERE space_id=?",
            (space_id,),
        )
        return int(row["c"]) if row is not None else 0

    async def upsert(self, zone: SpaceZone) -> None:
        await self._db.enqueue(
            """
            INSERT INTO space_zones(
                id, space_id, name, latitude, longitude,
                radius_m, color, created_by, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                latitude = excluded.latitude,
                longitude = excluded.longitude,
                radius_m = excluded.radius_m,
                color = excluded.color,
                updated_at = excluded.updated_at
            """,
            (
                zone.id,
                zone.space_id,
                zone.name,
                zone.latitude,
                zone.longitude,
                zone.radius_m,
                zone.color,
                zone.created_by,
                zone.created_at,
                zone.updated_at,
            ),
        )

    async def delete(self, zone_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM space_zones WHERE id=?",
            (zone_id,),
        )
