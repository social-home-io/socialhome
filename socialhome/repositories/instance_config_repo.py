"""Instance-config repository — the ``instance_config`` key/value table.

One row per key (``key TEXT PRIMARY KEY, value TEXT``). Callers namespace
their keys with a dotted prefix (``dashboard.settings``,
``dashboard.epoch`` …). Values are opaque strings — encoding (JSON, ints, hex)
is the owning service's concern.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase

_UPSERT = (
    "INSERT INTO instance_config(key, value) VALUES(?, ?) "
    "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
)


@runtime_checkable
class AbstractInstanceConfigRepo(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str) -> None: ...

    async def set_many(self, items: Mapping[str, str]) -> None: ...

    async def delete(self, key: str) -> None: ...


class SqliteInstanceConfigRepo:
    """SQLite-backed :class:`AbstractInstanceConfigRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def get(self, key: str) -> str | None:
        row = await self._db.fetchone(
            "SELECT value FROM instance_config WHERE key=?",
            (key,),
        )
        if row is None or row["value"] is None:
            return None
        return str(row["value"])

    async def set(self, key: str, value: str) -> None:
        await self._db.enqueue(
            _UPSERT,
            (key, value),
        )

    async def set_many(self, items: Mapping[str, str]) -> None:
        """Upsert every pair in one transaction — all land or none do."""
        rows = list(items.items())
        if not rows:
            return

        def _write(conn) -> None:
            conn.executemany(_UPSERT, rows)

        await self._db.transact(_write)

    async def delete(self, key: str) -> None:
        await self._db.enqueue(
            "DELETE FROM instance_config WHERE key=?",
            (key,),
        )
