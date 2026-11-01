"""App engine state (v_78) — one KEK-wrapped blob per (table, engine).

What an app engine (:mod:`socialhome.app_engines`) keeps on this household
for a group table: for the gift draw its settings, wish lists, this
household's keys and seeds per round, the public round data it checks
proofs against and its people's results. The value is opaque here: the
engine encrypts it with the household KEK (AAD bound to table and engine)
before it arrives. The row goes with its table (FK ``ON DELETE CASCADE``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..db.database import AsyncDatabase


@runtime_checkable
class AbstractAppEngineStateRepo(Protocol):
    async def get(self, session_id: str, engine: str) -> str | None: ...
    async def set(self, session_id: str, engine: str, value: str) -> None: ...


class SqliteAppEngineStateRepo:
    """SQLite-backed :class:`AbstractAppEngineStateRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def get(self, session_id: str, engine: str) -> str | None:
        """The engine's state for the table (KEK-wrapped JSON), or ``None``."""
        return await self._db.fetchval(
            "SELECT state_json FROM app_engine_state WHERE session_id = ? AND engine = ?",
            (session_id, engine),
        )

    async def set(self, session_id: str, engine: str, value: str) -> None:
        """Keep the engine's state for a table this household knows (no row
        for a table it doesn't: the foreign key refuses it)."""
        await self._db.enqueue(
            """INSERT INTO app_engine_state(session_id, engine, state_json, updated_at)
               VALUES(?, ?, ?, ?)
               ON CONFLICT(session_id, engine) DO UPDATE SET
                 state_json = excluded.state_json,
                 updated_at = excluded.updated_at""",
            (session_id, engine, value, datetime.now(UTC).isoformat()),
        )


__all__ = ["AbstractAppEngineStateRepo", "SqliteAppEngineStateRepo"]
