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

import logging
import uuid
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.federation import FederationEventType
from ..domain.federation_retention import (
    MAX_PENDING_PER_PEER,
    NEVER_DROP,
    retention_expires_at,
)
from .base import rows_to_dicts

log = logging.getLogger(__name__)

# Domain dataclass lives in ``socialhome/domain/outbox.py``;
# re-exported here so existing repo-level imports keep working.
from ..domain.outbox import OutboxEntry  # noqa: F401,E402


#: Default batch size for :meth:`SqliteOutboxRepo.purge_terminal`. Exported so
#: the OutboxProcessor's batch-drain loop compares against the SAME value its
#: per-call limit uses — the "fewer than a full batch ⇒ drained" sentinel must
#: not drift from the repo's default limit.
PURGE_BATCH = 5000


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
    async def purge_terminal(
        self, cutoff_iso: str, *, limit: int = PURGE_BATCH
    ) -> int: ...
    async def count_pending_for(self, instance_id: str) -> int: ...
    async def count_failed_for(self, instance_id: str) -> int: ...
    async def evict_oldest_droppable(self, instance_id: str) -> bool: ...


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
        # §4.4.7: default the retention deadline from the event type so every
        # AbstractOutboxRepo caller (today: send_event) gets a 7-day TTL on
        # ordinary events and NULL (retry forever) on NEVER_DROP ones. An
        # explicit ``expires_at`` from the caller is respected as-is.
        if expires_at is None:
            expires_at = retention_expires_at(event_type)
        # Per-peer back-pressure (§4.4.7): a permanently-offline peer plus a
        # busy space must not flood the outbox. At/over the cap, evict the
        # oldest droppable (non-NEVER_DROP) pending row to make room. We still
        # insert the new row — NEVER_DROP events are never refused, and for an
        # ordinary event the freshest state is the one worth keeping. If the
        # backlog is entirely NEVER_DROP (nothing droppable) we let it through
        # rather than drop a security/structural event.
        if await self.count_pending_for(instance_id) >= MAX_PENDING_PER_PEER:
            evicted = await self.evict_oldest_droppable(instance_id)
            if not evicted:
                log.warning(
                    "outbox: peer %s at pending cap with no droppable rows "
                    "(all NEVER_DROP) — inserting over cap",
                    instance_id,
                )
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
        # ``next_attempt_at`` and ``expires_at`` are stored as ISO-8601
        # with a ``T`` separator and a timezone suffix (``...+00:00``).
        # SQLite's ``datetime('now')`` returns ``YYYY-MM-DD HH:MM:SS``,
        # so a raw lexical compare disagrees on the separator and
        # treats no row as due. Wrapping both sides in ``datetime(...)``
        # normalises them to the same canonical form.
        rows = await self._db.fetchall(
            """
            SELECT * FROM federation_outbox
             WHERE status='pending'
               AND datetime(next_attempt_at) <= datetime('now')
               AND (
                   expires_at IS NULL
                   OR datetime(expires_at) > datetime('now')
               )
             ORDER BY datetime(next_attempt_at) ASC
             LIMIT ?
            """,
            (int(limit),),
        )
        return [_row_to_entry(d) for d in rows_to_dicts(rows)]

    async def mark_delivered(self, entry_id: str) -> None:
        # A delivered entry's job is done — remove it so the queue doesn't
        # accumulate one tombstone per successfully-delivered event. The
        # receiver's 2xx already satisfies at-least-once; nothing reads
        # delivered rows.
        await self._db.enqueue(
            "DELETE FROM federation_outbox WHERE id=?",
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

        Returns the count transitioned. Driven by ``OutboxProcessor`` on a
        periodic sweep (hourly by default) — receivers rebuild state via the
        sync protocols rather than the outbox replaying a week-stale event.
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

    async def purge_terminal(self, cutoff_iso: str, *, limit: int = PURGE_BATCH) -> int:
        """Delete up to ``limit`` terminal (delivered/failed) rows whose
        terminal timestamp is older than ``cutoff_iso``. Returns the count
        deleted. Batched so a large historical backlog drains over several
        sweep ticks instead of one writer-blocking transaction. Uses
        COALESCE(delivered_at, failed_at, created_at) so rows missing a
        terminal stamp (legacy) are still reclaimed by created_at.

        The COALESCE'd value is mixed-shape by construction —
        ``mark_failed`` writes SQLite's naive ``datetime('now')``,
        ``expire_past_retention`` writes Python tz-aware ISO, and
        ``created_at`` comes from the column DEFAULT — so both sides
        go through ``datetime()``. A raw TEXT compare sorts a naive
        same-day stamp below an ISO cutoff and would purge terminal
        rows before their grace window elapsed.
        """
        before = await self._db.fetchval(
            """
            SELECT COUNT(*) FROM federation_outbox
             WHERE status IN ('delivered','failed')
               AND datetime(COALESCE(delivered_at, failed_at, created_at))
                   < datetime(?)
            """,
            (cutoff_iso,),
            default=0,
        )
        to_delete = min(int(before), limit)
        if to_delete:
            await self._db.enqueue(
                """
                DELETE FROM federation_outbox
                 WHERE id IN (
                     SELECT id FROM federation_outbox
                      WHERE status IN ('delivered','failed')
                        AND datetime(
                            COALESCE(delivered_at, failed_at, created_at)
                        ) < datetime(?)
                      LIMIT ?
                 )
                """,
                (cutoff_iso, limit),
            )
        return to_delete

    async def count_pending_for(self, instance_id: str) -> int:
        return int(
            await self._db.fetchval(
                "SELECT COUNT(*) FROM federation_outbox "
                "WHERE instance_id=? AND status='pending'",
                (instance_id,),
                default=0,
            )
        )

    async def count_failed_for(self, instance_id: str) -> int:
        """Count envelopes permanently given up on for ``instance_id``.

        ``status='failed'`` is terminal — a PERMANENT 4xx rejection or an
        exhausted ``MAX_ATTEMPTS`` (see
        :mod:`infrastructure.outbox_processor`) — and is never retried; the
        rows are purged 24 h after going terminal. Reported next to
        :meth:`count_pending_for` so the admin surface can say "these were
        dropped" rather than implying the whole backlog is still in flight.
        Served by the same ``idx_federation_outbox_instance(instance_id,
        status)`` index as the pending count.
        """
        return int(
            await self._db.fetchval(
                "SELECT COUNT(*) FROM federation_outbox "
                "WHERE instance_id=? AND status='failed'",
                (instance_id,),
                default=0,
            )
        )

    async def evict_oldest_droppable(self, instance_id: str) -> bool:
        """Delete the oldest PENDING non-NEVER_DROP row for ``instance_id``.

        Returns True iff a row was evicted. NEVER_DROP rows (security /
        structural state) are excluded — they are never dropped to make room.
        """
        never = sorted(e.value for e in NEVER_DROP)
        placeholders = ",".join("?" * len(never))
        row = await self._db.fetchone(
            f"""
            SELECT id FROM federation_outbox
             WHERE instance_id=? AND status='pending'
               AND event_type NOT IN ({placeholders})
             ORDER BY datetime(created_at) ASC, id ASC
             LIMIT 1
            """,
            (instance_id, *never),
        )
        if row is None:
            return False
        await self._db.enqueue(
            "DELETE FROM federation_outbox WHERE id=?",
            (row["id"],),
        )
        return True


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
