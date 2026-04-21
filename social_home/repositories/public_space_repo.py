"""Public-space discovery repository — wraps public_space_cache + filters."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from .base import rows_to_dicts

# Domain dataclass lives in ``social_home/domain/public_space.py``;
# re-exported here so existing repo-level imports keep working.
from ..domain.public_space import PublicSpaceListing  # noqa: F401,E402


@runtime_checkable
class AbstractPublicSpaceRepo(Protocol):
    async def upsert(self, listing: PublicSpaceListing) -> None: ...
    async def list_active(self, *, limit: int = 50) -> list[PublicSpaceListing]: ...
    async def list_visible_for_user(
        self,
        user_id: str,
        *,
        limit: int = 50,
        max_min_age: int | None = None,
    ) -> list[PublicSpaceListing]: ...
    async def hide_for_user(self, user_id: str, space_id: str) -> None: ...
    async def block_instance(
        self, instance_id: str, *, blocked_by: str, reason: str | None = None
    ) -> None: ...
    async def is_instance_blocked(self, instance_id: str) -> bool: ...
    async def purge_older_than(self, cutoff_iso: str) -> int: ...


class SqlitePublicSpaceRepo:
    """SQLite-backed :class:`AbstractPublicSpaceRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def upsert(self, listing: PublicSpaceListing) -> None:
        cached = listing.cached_at or datetime.now(timezone.utc).isoformat()
        await self._db.enqueue(
            """
            INSERT INTO public_space_cache(
                space_id, instance_id, name, description, emoji,
                lat, lon, radius_km, member_count,
                min_age, target_audience, cached_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(space_id) DO UPDATE SET
                instance_id=excluded.instance_id,
                name=excluded.name,
                description=excluded.description,
                emoji=excluded.emoji,
                lat=excluded.lat,
                lon=excluded.lon,
                radius_km=excluded.radius_km,
                member_count=excluded.member_count,
                min_age=excluded.min_age,
                target_audience=excluded.target_audience,
                cached_at=excluded.cached_at
            """,
            (
                listing.space_id,
                listing.instance_id,
                listing.name,
                listing.description,
                listing.emoji,
                listing.lat,
                listing.lon,
                listing.radius_km,
                listing.member_count,
                int(listing.min_age or 0),
                listing.target_audience or "all",
                cached,
            ),
        )

    async def list_active(self, *, limit: int = 50) -> list[PublicSpaceListing]:
        rows = await self._db.fetchall(
            "SELECT * FROM public_space_cache"
            " WHERE instance_id NOT IN (SELECT instance_id FROM blocked_discover_instances)"
            " ORDER BY member_count DESC, cached_at DESC LIMIT ?",
            (limit,),
        )
        return [_row(r) for r in rows_to_dicts(rows)]

    async def list_visible_for_user(
        self,
        user_id: str,
        *,
        limit: int = 50,
        max_min_age: int | None = None,
    ) -> list[PublicSpaceListing]:
        """Return listings visible to *user_id* honouring age-gates.

        ``max_min_age`` caps the listing's ``min_age``: a caller with
        ``declared_age=12`` passes ``max_min_age=12`` and never sees
        spaces whose ``min_age`` exceeds that. ``None`` disables the
        filter for non-protected users and for adults (§CP.F1).
        """
        if max_min_age is None:
            rows = await self._db.fetchall(
                "SELECT psc.* FROM public_space_cache psc"
                " WHERE psc.instance_id NOT IN (SELECT instance_id FROM blocked_discover_instances)"
                " AND psc.space_id NOT IN (SELECT space_id FROM hidden_public_spaces WHERE user_id=?)"
                " ORDER BY psc.member_count DESC, psc.cached_at DESC LIMIT ?",
                (user_id, limit),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT psc.* FROM public_space_cache psc"
                " WHERE psc.instance_id NOT IN (SELECT instance_id FROM blocked_discover_instances)"
                " AND psc.space_id NOT IN (SELECT space_id FROM hidden_public_spaces WHERE user_id=?)"
                " AND psc.min_age <= ?"
                " ORDER BY psc.member_count DESC, psc.cached_at DESC LIMIT ?",
                (user_id, int(max_min_age), limit),
            )
        return [_row(r) for r in rows_to_dicts(rows)]

    async def hide_for_user(self, user_id: str, space_id: str) -> None:
        await self._db.enqueue(
            "INSERT OR IGNORE INTO hidden_public_spaces(user_id, space_id) VALUES(?, ?)",
            (user_id, space_id),
        )

    async def block_instance(
        self,
        instance_id: str,
        *,
        blocked_by: str,
        reason: str | None = None,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO blocked_discover_instances(instance_id, blocked_by, reason)
            VALUES(?, ?, ?)
            ON CONFLICT(instance_id) DO UPDATE SET
                blocked_by=excluded.blocked_by, reason=excluded.reason
            """,
            (instance_id, blocked_by, reason),
        )

    async def is_instance_blocked(self, instance_id: str) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM blocked_discover_instances WHERE instance_id=?",
            (instance_id,),
        )
        return row is not None

    async def purge_older_than(self, cutoff_iso: str) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM public_space_cache WHERE cached_at < ?",
            (cutoff_iso,),
        )
        n = int(row["n"]) if row else 0
        if n:
            await self._db.enqueue(
                "DELETE FROM public_space_cache WHERE cached_at < ?",
                (cutoff_iso,),
            )
        return n


def _row(r) -> PublicSpaceListing:
    return PublicSpaceListing(
        space_id=r["space_id"],
        instance_id=r["instance_id"],
        name=r["name"],
        description=r["description"],
        emoji=r["emoji"],
        lat=r["lat"],
        lon=r["lon"],
        radius_km=r["radius_km"],
        member_count=int(r["member_count"] or 0),
        cached_at=r["cached_at"],
        min_age=int(_get(r, "min_age") or 0),
        target_audience=(_get(r, "target_audience") or "all"),
    )


def _get(row, key: str):
    try:
        return row[key]
    except KeyError, IndexError:
        return None
