"""Household-zone repository — the household's own zone list (migration 0097).

Local-only rows (never federated, never sync-stamped). The DB enforces
the radius range, one Home zone, case-insensitive unique names and
unique ``external_id`` values; this module adds no business logic.

Writes that touch more than one row (moving the Home flag, mirroring a
platform's zone set) run in one ``BEGIN IMMEDIATE`` transaction via
:meth:`AsyncDatabase.transact` so a reader never sees two Home zones or
a half-applied sync.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.household_zone import HouseholdZone
from .base import row_to_dict, rows_to_dicts

__all__ = ["AbstractHouseholdZoneRepo", "SqliteHouseholdZoneRepo"]


@runtime_checkable
class AbstractHouseholdZoneRepo(Protocol):
    async def list(self) -> list[HouseholdZone]:
        """Every zone — the Home zone first, then by name (A–Z, any case)."""
        ...

    async def get(self, zone_id: str) -> HouseholdZone | None: ...
    async def get_by_name(self, name: str) -> HouseholdZone | None:
        """Case-insensitive name lookup."""
        ...

    async def get_by_external_id(self, external_id: str) -> HouseholdZone | None: ...
    async def count(self) -> int: ...
    async def create(self, zone: HouseholdZone) -> None:
        """Insert ``zone``; when it is the Home zone the old Home loses the
        flag in the same transaction."""
        ...

    async def update(self, zone: HouseholdZone) -> bool:
        """Rewrite the row with ``zone.id`` (``created_at`` and
        ``external_id`` are kept). ``False`` = no such row."""
        ...

    async def delete(self, zone_id: str) -> bool: ...
    async def mirror_external(self, zones: Sequence[HouseholdZone]) -> tuple[int, int]:
        """Make the platform-synced rows exactly ``zones`` (matched on
        ``external_id``), atomically. Rows without an ``external_id`` are
        never touched, except that an incoming Home zone takes the Home
        flag. Returns ``(upserted, removed)``."""
        ...


_COLS = (
    "id, name, latitude, longitude, radius_m, color, icon, is_home,"
    " external_id, created_at, updated_at"
)
_ORDER = " ORDER BY is_home DESC, name COLLATE NOCASE, id"


def _row_to_zone(row: dict[str, Any] | None) -> HouseholdZone | None:
    if row is None:
        return None
    return HouseholdZone(
        id=row["id"],
        name=row["name"],
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
        radius_m=int(row["radius_m"]),
        color=row["color"],
        icon=row["icon"],
        is_home=bool(row["is_home"]),
        external_id=row["external_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _params(z: HouseholdZone) -> tuple[Any, ...]:
    return (
        z.id,
        z.name,
        z.latitude,
        z.longitude,
        z.radius_m,
        z.color,
        z.icon,
        1 if z.is_home else 0,
        z.external_id,
        z.created_at,
        z.updated_at,
    )


def _clear_home(conn: sqlite3.Connection, *, except_id: str) -> None:
    conn.execute(
        "UPDATE household_zones SET is_home=0 WHERE is_home=1 AND id<>?",
        (except_id,),
    )


class SqliteHouseholdZoneRepo:
    """SQLite-backed implementation of :class:`AbstractHouseholdZoneRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def list(self) -> list[HouseholdZone]:
        rows = await self._db.fetchall(f"SELECT {_COLS} FROM household_zones{_ORDER}")
        return [z for z in map(_row_to_zone, rows_to_dicts(rows)) if z is not None]

    async def get(self, zone_id: str) -> HouseholdZone | None:
        row = await self._db.fetchone(
            f"SELECT {_COLS} FROM household_zones WHERE id=?", (zone_id,)
        )
        return _row_to_zone(row_to_dict(row))

    async def get_by_name(self, name: str) -> HouseholdZone | None:
        # ``name`` is declared ``COLLATE NOCASE``, so ``=`` folds case.
        row = await self._db.fetchone(
            f"SELECT {_COLS} FROM household_zones WHERE name=?", (name,)
        )
        return _row_to_zone(row_to_dict(row))

    async def get_by_external_id(self, external_id: str) -> HouseholdZone | None:
        row = await self._db.fetchone(
            f"SELECT {_COLS} FROM household_zones WHERE external_id=?",
            (external_id,),
        )
        return _row_to_zone(row_to_dict(row))

    async def count(self) -> int:
        row = await self._db.fetchone("SELECT COUNT(*) AS c FROM household_zones")
        return int(row["c"]) if row is not None else 0

    async def create(self, zone: HouseholdZone) -> None:
        def _run(conn: sqlite3.Connection) -> None:
            if zone.is_home:
                _clear_home(conn, except_id=zone.id)
            conn.execute(
                f"INSERT INTO household_zones({_COLS})"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _params(zone),
            )

        await self._db.transact(_run)

    async def update(self, zone: HouseholdZone) -> bool:
        def _run(conn: sqlite3.Connection) -> bool:
            exists = conn.execute(
                "SELECT 1 FROM household_zones WHERE id=?", (zone.id,)
            ).fetchone()
            if exists is None:
                return False
            if zone.is_home:
                _clear_home(conn, except_id=zone.id)
            conn.execute(
                "UPDATE household_zones SET name=?, latitude=?, longitude=?,"
                " radius_m=?, color=?, icon=?, is_home=?, updated_at=?"
                " WHERE id=?",
                (
                    zone.name,
                    zone.latitude,
                    zone.longitude,
                    zone.radius_m,
                    zone.color,
                    zone.icon,
                    1 if zone.is_home else 0,
                    zone.updated_at,
                    zone.id,
                ),
            )
            return True

        result: bool = await self._db.transact(_run)
        return result

    async def delete(self, zone_id: str) -> bool:
        n = await self._db.enqueue_rowcount(
            "DELETE FROM household_zones WHERE id=?", (zone_id,)
        )
        return n > 0

    async def mirror_external(self, zones: Sequence[HouseholdZone]) -> tuple[int, int]:
        incoming = [z for z in zones if z.external_id is not None]

        def _run(conn: sqlite3.Connection) -> tuple[int, int]:
            keep = [z.external_id for z in incoming]
            marks = ",".join("?" * len(keep))
            removed = conn.execute(
                "DELETE FROM household_zones WHERE external_id IS NOT NULL"
                + (f" AND external_id NOT IN ({marks})" if keep else ""),
                keep,
            ).rowcount
            # Park the surviving synced rows on a name no zone may hold
            # (NUL + id — names refuse control characters) and drop their
            # Home flag, so two zones swapping names, or the Home moving
            # between synced zones, never trips a UNIQUE index midway.
            conn.execute(
                "UPDATE household_zones SET name=char(1) || id, is_home=0"
                " WHERE external_id IS NOT NULL"
            )
            if any(z.is_home for z in incoming):
                conn.execute("UPDATE household_zones SET is_home=0 WHERE is_home=1")
            for z in incoming:
                conn.execute(
                    f"INSERT INTO household_zones({_COLS})"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(external_id) DO UPDATE SET"
                    " name=excluded.name, latitude=excluded.latitude,"
                    " longitude=excluded.longitude, radius_m=excluded.radius_m,"
                    " color=excluded.color, icon=excluded.icon,"
                    " is_home=excluded.is_home, updated_at=excluded.updated_at",
                    _params(z),
                )
            return len(incoming), int(removed)

        result: tuple[int, int] = await self._db.transact(_run)
        return result
