"""Pairing relay repository — durable backing store for §11.9
``PAIRING_INTRO_RELAY`` requests waiting on admin approval.

Replaces the earlier in-memory dict so a restart no longer drops the
queue. Each row carries a ``status`` (pending / approved / declined)
which lets a retention sweeper expire old rows without losing
admin-visible state mid-flight.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from .base import row_to_dict, rows_to_dicts


@runtime_checkable
class AbstractPairingRelayRepo(Protocol):
    async def save(
        self,
        *,
        request_id: str,
        from_instance: str,
        target_instance_id: str,
        message: str,
        received_at: datetime,
    ) -> None: ...
    async def get(self, request_id: str) -> dict | None: ...
    async def list_pending(self) -> list[dict]: ...
    async def set_status(self, request_id: str, status: str) -> None: ...
    async def count_pending(self) -> int: ...
    async def delete_oldest_pending(self, keep: int) -> int: ...
    async def delete_older_than(self, *, status: str, cutoff_iso: str) -> int: ...


class SqlitePairingRelayRepo:
    """SQLite-backed :class:`AbstractPairingRelayRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def save(
        self,
        *,
        request_id: str,
        from_instance: str,
        target_instance_id: str,
        message: str,
        received_at: datetime,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO pairing_relay(
                id, from_instance, target_instance_id, message, received_at
            ) VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                request_id,
                from_instance,
                target_instance_id,
                message,
                received_at.astimezone(timezone.utc).isoformat(),
            ),
        )

    async def get(self, request_id: str) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM pairing_relay WHERE id=? AND status='pending'",
            (request_id,),
        )
        return row_to_dict(row)

    async def list_pending(self) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM pairing_relay WHERE status='pending' ORDER BY received_at",
        )
        return rows_to_dicts(rows)

    async def set_status(self, request_id: str, status: str) -> None:
        await self._db.enqueue(
            "UPDATE pairing_relay SET status=? WHERE id=?",
            (status, request_id),
        )

    async def count_pending(self) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM pairing_relay WHERE status='pending'",
        )
        return int(row["n"]) if row else 0

    async def delete_oldest_pending(self, keep: int) -> int:
        """Trim ``pending`` rows to the ``keep`` most recent. Returns the
        number deleted. Used to enforce the §11.9 anti-DoS cap."""
        row = await self._db.fetchone(
            """
            SELECT received_at FROM pairing_relay WHERE status='pending'
             ORDER BY received_at DESC LIMIT 1 OFFSET ?
            """,
            (int(keep),),
        )
        if row is None:
            return 0
        cutoff = row["received_at"]
        before = await self._db.fetchval(
            "SELECT COUNT(*) FROM pairing_relay "
            "WHERE status='pending' AND received_at <= ?",
            (cutoff,),
            default=0,
        )
        await self._db.enqueue(
            "DELETE FROM pairing_relay WHERE status='pending' AND received_at <= ?",
            (cutoff,),
        )
        return int(before)

    async def delete_older_than(self, *, status: str, cutoff_iso: str) -> int:
        """Delete rows in *status* with ``received_at < cutoff_iso``.
        Returns the count purged. Used by
        :class:`PairingRelayRetentionScheduler`.
        """
        before = await self._db.fetchval(
            "SELECT COUNT(*) FROM pairing_relay WHERE status=? AND received_at < ?",
            (status, cutoff_iso),
            default=0,
        )
        await self._db.enqueue(
            "DELETE FROM pairing_relay WHERE status=? AND received_at < ?",
            (status, cutoff_iso),
        )
        return int(before)
