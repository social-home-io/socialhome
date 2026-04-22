"""Per-space content encryption keys (§4.3, §25.8.20–21).

Each space has one or more **epoch** keys. An epoch is incremented when
the key is rotated (member ban, admin departure, scheduled rekey). All
content for that epoch is encrypted under that key; readers select the
key by epoch number on the inbound envelope.

Keys are stored KEK-encrypted (see :class:`KeyManager`). The repository
returns the ciphertext as-is — service code calls
:meth:`KeyManager.decrypt` to obtain the raw key bytes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from .base import rows_to_dicts

# Domain dataclass lives in ``socialhome/domain/space_key.py``;
# re-exported here so existing repo-level imports keep working.
from ..domain.space_key import SpaceKey  # noqa: F401,E402


@runtime_checkable
class AbstractSpaceKeyRepo(Protocol):
    async def save(self, key: SpaceKey) -> None: ...
    async def get(self, space_id: str, epoch: int) -> SpaceKey | None: ...
    async def get_latest(self, space_id: str) -> SpaceKey | None: ...
    async def list_for_space(self, space_id: str) -> list[SpaceKey]: ...
    async def next_epoch(self, space_id: str) -> int: ...


class SqliteSpaceKeyRepo:
    """SQLite-backed :class:`AbstractSpaceKeyRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def save(self, key: SpaceKey) -> None:
        created = key.created_at or datetime.now(timezone.utc).isoformat()
        await self._db.enqueue(
            """
            INSERT INTO space_keys(space_id, epoch, content_key_hex, created_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(space_id, epoch) DO UPDATE SET
                content_key_hex=excluded.content_key_hex
            """,
            (key.space_id, key.epoch, key.content_key_hex, created),
        )

    async def get(self, space_id: str, epoch: int) -> SpaceKey | None:
        row = await self._db.fetchone(
            "SELECT * FROM space_keys WHERE space_id=? AND epoch=?",
            (space_id, epoch),
        )
        return _row(row) if row else None

    async def get_latest(self, space_id: str) -> SpaceKey | None:
        row = await self._db.fetchone(
            "SELECT * FROM space_keys WHERE space_id=? ORDER BY epoch DESC LIMIT 1",
            (space_id,),
        )
        return _row(row) if row else None

    async def list_for_space(self, space_id: str) -> list[SpaceKey]:
        rows = await self._db.fetchall(
            "SELECT * FROM space_keys WHERE space_id=? ORDER BY epoch",
            (space_id,),
        )
        return [_row(r) for r in rows_to_dicts(rows)]

    async def next_epoch(self, space_id: str) -> int:
        row = await self._db.fetchone(
            "SELECT COALESCE(MAX(epoch), -1) AS m FROM space_keys WHERE space_id=?",
            (space_id,),
        )
        return int(row["m"]) + 1 if row else 0


def _row(row) -> SpaceKey:
    return SpaceKey(
        space_id=row["space_id"],
        epoch=int(row["epoch"]),
        content_key_hex=row["content_key_hex"],
        created_at=row["created_at"],
    )
