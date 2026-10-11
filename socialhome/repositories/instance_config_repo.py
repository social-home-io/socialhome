"""Instance-config repository — the ``instance_config`` key/value table.

One row per key (``key TEXT PRIMARY KEY, value TEXT``). Callers namespace
their keys with a dotted prefix (``dashboard.settings``,
``dashboard.epoch`` …) so :meth:`get_many` can read one feature's rows in
a single query. Values are opaque strings — encoding (JSON, ints, hex)
is the owning service's concern.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase


@runtime_checkable
class AbstractInstanceConfigRepo(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def get_many(self, prefix: str) -> dict[str, str]: ...

    async def set(self, key: str, value: str) -> None: ...

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

    async def get_many(self, prefix: str) -> dict[str, str]:
        """Every row whose key starts with ``prefix`` (matched literally —
        ``substr`` instead of ``LIKE`` so ``%`` / ``_`` are not wildcards)."""
        rows = await self._db.fetchall(
            "SELECT key, value FROM instance_config "
            "WHERE substr(key, 1, ?) = ? AND value IS NOT NULL",
            (len(prefix), prefix),
        )
        return {str(r["key"]): str(r["value"]) for r in rows}

    async def set(self, key: str, value: str) -> None:
        await self._db.enqueue(
            "INSERT INTO instance_config(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    async def delete(self, key: str) -> None:
        await self._db.enqueue(
            "DELETE FROM instance_config WHERE key=?",
            (key,),
        )
