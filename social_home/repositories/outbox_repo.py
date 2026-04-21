"""Federation outbox repository (§5.2 pattern ②).

The outbox is the persistence backbone for reliable federation delivery.
Service-layer code writes entries in the same DB batch as the mutation they
describe; the :class:`OutboxProcessor` background task (see
:mod:`infrastructure.outbox_processor`) drives delivery with jittered
exponential backoff.

Retention tiers follow §4.4.7:

* **Structural** events retain for 90 days; receivers rebuild via
  ``SPACE_SYNC_RESUME`` if they miss older events.
* **Security-critical** events (ban / unban, admin key share, UNPAIR,
  rekey) never expire; they are retained past 90 days and delivered first
  on reconnect.
* **Regular** events expire after 7 days; receivers rebuild state from
  their own DB via sync protocols when coming back online.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.federation import FederationEventType
from .base import rows_to_dicts

# Domain dataclass lives in ``social_home/domain/outbox.py``;
# re-exported here so existing repo-level imports keep working.
from ..domain.outbox import OutboxEntry  # noqa: F401,E402


@runtime_checkable
class AbstractOutboxRepo(Protocol):
    async def enqueue(
        self,
        *,
        instance_id: str,
        event_type: FederationEventType,
        payload_json: str,
        msg_id: str | None = None,
        authority_json: str | None = None,
        expires_at: str | None = None,
    ) -> str: ...

    async def list_due(self, limit: int = 50) -> list[OutboxEntry]: ...
    async def mark_delivered(self, entry_id: str) -> None: ...
    async def mark_failed(self, entry_id: str) -> None: ...
    async def reschedule(
        self,
        entry_id: str,
        next_attempt_at: str,
        attempts: int,
    ) -> None: ...
    async def expire_past_retention(self, now_iso: str) -> int: ...
    async def count_pending_for(self, instance_id: str) -> int: ...


class SqliteOutboxRepo:
    """SQLite-backed :class:`AbstractOutboxRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def enqueue(
        self,
        *,
        instance_id: str,
        event_type: FederationEventType,
        payload_json: str,
        msg_id: str | None = None,
        authority_json: str | None = None,
        expires_at: str | None = None,
    ) -> str:
        entry_id = msg_id or uuid.uuid4().hex
        await self._db.enqueue(
            """
            INSERT INTO federation_outbox(
                id, instance_id, event_type, payload_json,
                authority_json, expires_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                entry_id,
                instance_id,
                event_type.value,
                payload_json,
                authority_json,
                expires_at,
            ),
        )
        return entry_id

    async def list_due(self, limit: int = 50) -> list[OutboxEntry]:
        """Return pending entries whose ``next_attempt_at`` is due.

        Excludes entries past their retention window (``expires_at``).
        The caller is responsible for filtering entries whose destination
        instance is banned or unreachable.
        """
        rows = await self._db.fetchall(
            """
            SELECT * FROM federation_outbox
             WHERE status='pending'
               AND next_attempt_at <= datetime('now')
               AND (expires_at IS NULL OR expires_at > datetime('now'))
             ORDER BY next_attempt_at ASC
             LIMIT ?
            """,
            (int(limit),),
        )
        return [_row_to_entry(d) for d in rows_to_dicts(rows)]

    async def mark_delivered(self, entry_id: str) -> None:
        await self._db.enqueue(
            "UPDATE federation_outbox SET status='delivered', "
            "delivered_at=datetime('now') WHERE id=?",
            (entry_id,),
        )

    async def mark_failed(self, entry_id: str) -> None:
        await self._db.enqueue(
            "UPDATE federation_outbox SET status='failed', "
            "failed_at=datetime('now') WHERE id=?",
            (entry_id,),
        )

    async def reschedule(
        self,
        entry_id: str,
        next_attempt_at: str,
        attempts: int,
    ) -> None:
        await self._db.enqueue(
            "UPDATE federation_outbox SET next_attempt_at=?, attempts=? WHERE id=?",
            (next_attempt_at, attempts, entry_id),
        )

    async def expire_past_retention(self, now_iso: str) -> int:
        """Mark pending entries whose ``expires_at`` has passed as ``failed``.

        Returns the count transitioned. Runs on a daily schedule —
        receivers will rebuild state via sync protocols.
        """
        count = await self._db.fetchval(
            """
            SELECT COUNT(*) FROM federation_outbox
             WHERE status='pending'
               AND expires_at IS NOT NULL
               AND expires_at < ?
            """,
            (now_iso,),
            default=0,
        )
        await self._db.enqueue(
            """
            UPDATE federation_outbox SET status='failed', failed_at=?
             WHERE status='pending'
               AND expires_at IS NOT NULL
               AND expires_at < ?
            """,
            (now_iso, now_iso),
        )
        return int(count)

    async def count_pending_for(self, instance_id: str) -> int:
        return int(
            await self._db.fetchval(
                "SELECT COUNT(*) FROM federation_outbox "
                "WHERE instance_id=? AND status='pending'",
                (instance_id,),
                default=0,
            )
        )


def _row_to_entry(row: dict) -> OutboxEntry:
    return OutboxEntry(
        id=row["id"],
        instance_id=row["instance_id"],
        event_type=FederationEventType(row["event_type"]),
        payload_json=row["payload_json"],
        status=row.get("status", "pending"),
        attempts=int(row.get("attempts") or 0),
        next_attempt_at=row["next_attempt_at"],
        created_at=row["created_at"],
        authority_json=row.get("authority_json"),
        expires_at=row.get("expires_at"),
        delivered_at=row.get("delivered_at"),
        failed_at=row.get("failed_at"),
    )
