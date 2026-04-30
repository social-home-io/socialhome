"""Household feature toggles repository (§22).

Wraps the SQL surface used by :class:`HouseholdFeaturesService` so the
service depends only on the abstract protocol — never on raw SQL or
the SQLite implementation.

Only the singleton ``id='default'`` row is touched.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.household_features import HouseholdFeatures


_FEATURE_KEYS: tuple[str, ...] = (
    "feat_feed",
    "feat_pages",
    "feat_tasks",
    "feat_stickies",
    "feat_calendar",
)
_ALLOW_KEYS: tuple[str, ...] = (
    "allow_text",
    "allow_image",
    "allow_video",
    "allow_file",
    "allow_poll",
    "allow_schedule",
    "allow_location",
)
ALL_KEYS: frozenset[str] = frozenset(_FEATURE_KEYS + _ALLOW_KEYS)


@runtime_checkable
class AbstractHouseholdFeaturesRepo(Protocol):
    async def get(self) -> HouseholdFeatures: ...
    async def ensure_row(self) -> None: ...
    async def set_household_name(self, name: str) -> None: ...
    async def set_toggle(self, key: str, value: bool) -> None: ...


class SqliteHouseholdFeaturesRepo:
    """SQLite-backed :class:`AbstractHouseholdFeaturesRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def get(self) -> HouseholdFeatures:
        row = await self._db.fetchone(
            "SELECT * FROM household_features WHERE id='default'",
        )
        if row is None:
            return HouseholdFeatures()
        kwargs = {
            "household_name": row["household_name"],
            **{k: bool(row[k]) for k in _FEATURE_KEYS},
            **{k: bool(row[k]) for k in _ALLOW_KEYS},
        }
        return HouseholdFeatures(**kwargs)

    async def ensure_row(self) -> None:
        await self._db.enqueue(
            "INSERT OR IGNORE INTO household_features(id) VALUES('default')",
        )

    async def set_household_name(self, name: str) -> None:
        await self._db.enqueue(
            "UPDATE household_features SET household_name=? WHERE id='default'",
            (name,),
        )

    async def set_toggle(self, key: str, value: bool) -> None:
        if key not in ALL_KEYS:
            raise KeyError(f"unknown household-features toggle: {key!r}")
        # Static allow-list above gates the column name — no SQL injection.
        await self._db.enqueue(
            f"UPDATE household_features SET {key}=? WHERE id='default'",
            (int(value),),
        )
