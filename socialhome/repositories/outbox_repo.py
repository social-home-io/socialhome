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
import sqlite3
import uuid
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

import orjson

from ..db import AsyncDatabase
from ..domain import federation_retention
from ..domain.federation import FederationEventType, PairingStatus
from ..domain.federation_retention import (
    ANY_RELAYED_DELETE_KEY_PREFIX,
    MAX_PENDING_PER_PEER,
    NEVER_DROP,
    RELAYED_DELETE_KEY_PREFIX,
    RETENTION_DAYS,
    capped_delete_key,
    retention_expires_at,
)

# Domain dataclasses live in ``socialhome/domain/outbox.py``; re-exported
# here so existing repo-level imports keep working.
from ..domain.outbox import OutboxEntry, OutboxPayload
from .base import rows_to_dicts

log = logging.getLogger(__name__)

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
        supersede_key: str | None = None,
        payload_ref: OutboxPayload | None = None,
    ) -> str | None: ...

    async def enqueue_refs(
        self,
        *,
        instance_ids: list[str],
        event_type: FederationEventType,
        supersede_key: str | None,
        payload_ref: OutboxPayload,
    ) -> dict[str, str]: ...

    async def get_payload(self, payload_id: str) -> OutboxPayload | None: ...

    async def drop_superseded(self, instance_id: str, supersede_key: str) -> int: ...
    async def drop_pending_with_key_prefix(
        self, instance_id: str, prefix: str
    ) -> int: ...
    async def list_due(
        self, limit: int = 50, *, exclude: Collection[str] = ()
    ) -> list[OutboxEntry]: ...
    async def pending_multi_due(self, instance_id: str) -> str | None: ...
    async def ungroup_payload(self, payload_id: str) -> None: ...
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
    async def count_pending_with_key_prefix(
        self, instance_id: str, prefix: str
    ) -> int: ...
    async def evict_oldest_droppable(self, instance_id: str) -> bool: ...
    async def delete_for_instance(self, instance_id: str) -> None: ...
    async def delete_pending_of_types(
        self,
        instance_id: str,
        event_types: frozenset[FederationEventType],
    ) -> int: ...
    async def purge_orphaned(self) -> int: ...
    async def expedite(
        self,
        instance_id: str,
        event_type: FederationEventType,
    ) -> bool: ...


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
        supersede_key: str | None = None,
        payload_ref: OutboxPayload | None = None,
    ) -> str | None:
        """Queue one signed envelope for ``instance_id``; return its id.

        With a ``payload_ref`` (migration 0145) the row stores no envelope
        (``payload_json`` is ``''``): it references *payload_ref*, stored
        once — ``INSERT OR IGNORE``, so every recipient of one broadcast
        shares it — and sealed for this peer when it is sent. The payload
        and the row go in in one transaction; a row refused below takes an
        unreferenced payload with it, and a trigger deletes the payload when
        its last row goes.

        With a ``supersede_key`` (latest-only state, see
        :func:`~socialhome.domain.federation_retention.supersede_key`) the
        peer's pending row with the same key is replaced, not added to — so
        it never counts against the per-peer cap twice.

        Returns ``None`` — and writes nothing — when we no longer hold a
        pairing the envelope could ever be delivered to: the
        ``remote_instances`` row is gone (unpaired), or it is an unpair
        tombstone and this is anything but our ``UNPAIR``. The check runs
        inside the INSERT itself, so it is serialized with the unpair's
        purge on the single DB writer: a send that looked the peer up,
        awaited its transport while the pairing ended, and only then
        reached here can no longer strand a row behind the purge.
        """
        entry_id = msg_id or uuid.uuid4().hex
        supersede_key, expires_at = await self._make_room(
            instance_id, event_type, supersede_key, expires_at
        )
        params = _row_params(
            entry_id,
            instance_id,
            event_type,
            payload_json,
            authority_json,
            expires_at,
            supersede_key,
            payload_ref,
        )
        if payload_ref is None:
            inserted = await self._db.enqueue_rowcount(_INSERT_ROW, params)
        else:
            inserted = await self._db.transact(
                lambda conn: _insert_refs(conn, payload_ref, [params])
            )
        if not inserted:
            log.info(
                "outbox: %s is no longer paired — not queueing %s",
                instance_id,
                event_type.value,
            )
            return None
        return entry_id

    async def enqueue_refs(
        self,
        *,
        instance_ids: list[str],
        event_type: FederationEventType,
        supersede_key: str | None,
        payload_ref: OutboxPayload,
    ) -> dict[str, str]:
        """Store *payload_ref* once and one ref row per household of
        *instance_ids* in ONE transaction (v_91: a broadcast's multi
        payload). Each row is prepared as :meth:`enqueue` prepares one
        (supersede, per-peer cap); a household no longer paired gets none.
        Returns ``{instance_id: row id}`` of the rows written."""
        rows: list[tuple] = []
        for instance_id in instance_ids:
            key, expires_at = await self._make_room(
                instance_id, event_type, supersede_key, None
            )
            rows.append(
                _row_params(
                    uuid.uuid4().hex,
                    instance_id,
                    event_type,
                    "",
                    None,
                    expires_at,
                    key,
                    payload_ref,
                )
            )
        written = await self._db.transact(
            lambda conn: _insert_refs(conn, payload_ref, rows)
        )
        return {params[1]: params[0] for params in written}

    async def _make_room(
        self,
        instance_id: str,
        event_type: FederationEventType,
        supersede_key: str | None,
        expires_at: str | None,
    ) -> tuple[str | None, str | None]:
        """Before a row for *instance_id* goes in: its retention class and
        deadline, the pending row it supersedes dropped, and room made under
        the per-peer cap. Returns the row's ``(supersede_key, expires_at)``."""
        if (
            supersede_key is not None
            and supersede_key.startswith(RELAYED_DELETE_KEY_PREFIX)
            and expires_at is None
        ):
            supersede_key, expires_at = await self._relayed_delete_class(
                instance_id, supersede_key
            )
        # §4.4.7: default the retention deadline from the event type so every
        # AbstractOutboxRepo caller (today: send_event) gets a 7-day TTL on
        # ordinary events and NULL (retry forever) on NEVER_DROP ones. An
        # explicit ``expires_at`` from the caller is respected as-is.
        if expires_at is None:
            expires_at = retention_expires_at(event_type)
        if supersede_key is not None:
            # Before the cap check: the row this one replaces must not make
            # the cap evict someone's DM to make room for its successor.
            await self.drop_superseded(instance_id, supersede_key)
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
        return supersede_key, expires_at

    async def _relayed_delete_class(
        self, instance_id: str, key: str
    ) -> tuple[str, str | None]:
        """The key and expiry a delete relayed for another origin is queued
        under: long-lived (as is) while that origin holds fewer than
        :data:`~socialhome.domain.federation_retention
        .MAX_LONG_LIVED_RELAYED_PER_ORIGIN` pending rows for *instance_id*,
        otherwise the ordinary class (adversarial review F2) — a household
        signing deletes for made-up ids can't pin a peer's queue for a month."""
        origin, _, moment_id = key[len(RELAYED_DELETE_KEY_PREFIX) :].partition(":")
        held = await self.count_pending_with_key_prefix(
            instance_id, f"{RELAYED_DELETE_KEY_PREFIX}{origin}:"
        )
        if held < federation_retention.MAX_LONG_LIVED_RELAYED_PER_ORIGIN:
            return key, None
        ordinary = datetime.now(UTC) + timedelta(days=RETENTION_DAYS)
        return capped_delete_key(origin, moment_id), ordinary.isoformat()

    async def count_pending_with_key_prefix(self, instance_id: str, prefix: str) -> int:
        """Pending rows for *instance_id* whose key starts with *prefix*
        (a range over ``idx_federation_outbox_supersede``)."""
        return int(
            await self._db.fetchval(
                "SELECT COUNT(*) FROM federation_outbox WHERE instance_id=?"
                " AND supersede_key >= ? AND supersede_key < ?"
                " AND status='pending'",
                (instance_id, prefix, prefix + "\uffff"),
                default=0,
            )
        )

    async def drop_pending_with_key_prefix(self, instance_id: str, prefix: str) -> int:
        """Delete ``instance_id``'s PENDING rows whose key starts with
        *prefix*; return how many (a range over
        ``idx_federation_outbox_supersede``, read first like
        :meth:`drop_superseded`)."""
        if not await self.count_pending_with_key_prefix(instance_id, prefix):
            return 0
        return await self._db.enqueue_rowcount(
            "DELETE FROM federation_outbox WHERE instance_id=?"
            " AND supersede_key >= ? AND supersede_key < ?"
            " AND status='pending'",
            (instance_id, prefix, prefix + "\uffff"),
        )

    async def drop_superseded(self, instance_id: str, supersede_key: str) -> int:
        """Delete ``instance_id``'s PENDING row(s) carrying ``supersede_key``;
        return how many.

        Read first (served by the partial ``idx_federation_outbox_supersede``
        index) so the common case — nothing queued for that peer, every send
        of a moving pin — costs a read, not a write on the single writer.
        """
        queued = await self._db.fetchval(
            "SELECT COUNT(*) FROM federation_outbox"
            " WHERE instance_id=? AND supersede_key=? AND status='pending'",
            (instance_id, supersede_key),
            default=0,
        )
        if not queued:
            return 0
        return await self._db.enqueue_rowcount(
            "DELETE FROM federation_outbox"
            " WHERE instance_id=? AND supersede_key=? AND status='pending'",
            (instance_id, supersede_key),
        )

    async def list_due(
        self, limit: int = 50, *, exclude: Collection[str] = ()
    ) -> list[OutboxEntry]:
        """Return pending entries whose ``next_attempt_at`` is due.

        Excludes entries past their retention window (``expires_at``) and
        those of the households in *exclude* (the processor's busy
        recipients, so their backlog never fills the batch). A row that is
        not of a multi payload is left out while an older row of a multi
        payload is still pending for its household (v_91 per-recipient
        FIFO — on every retry, not only the first attempt). Ties on the due
        time go oldest first, then in insert order. A multi payload taken
        out of every group (:meth:`ungroup_payload`) lists with no
        ``msg_id``. The caller is responsible for filtering entries whose
        destination instance is banned or unreachable.
        """
        # ``next_attempt_at`` and ``expires_at`` are stored as ISO-8601
        # with a ``T`` separator and a timezone suffix (``...+00:00``).
        # SQLite's ``datetime('now')`` returns ``YYYY-MM-DD HH:MM:SS``,
        # so a raw lexical compare disagrees on the separator and
        # treats no row as due. Wrapping both sides in ``datetime(...)``
        # normalises them to the same canonical form.
        rows = await self._db.fetchall(
            """
            SELECT o.*, p.space_id AS payload_space_id,
                   CASE WHEN p.per_row = 0 THEN p.msg_id END AS payload_msg_id
              FROM federation_outbox o
              LEFT JOIN federation_outbox_payloads p ON p.id = o.payload_id
             WHERE o.status='pending'
               AND datetime(o.next_attempt_at) <= datetime('now')
               AND (
                   o.expires_at IS NULL
                   OR datetime(o.expires_at) > datetime('now')
               )
               AND o.instance_id NOT IN (SELECT value FROM json_each(?))
               AND (
                   p.msg_id IS NOT NULL
                   OR NOT EXISTS (
                       SELECT 1
                         FROM federation_outbox m
                         JOIN federation_outbox_payloads mp ON mp.id = m.payload_id
                        WHERE m.instance_id = o.instance_id
                          AND m.status = 'pending'
                          AND mp.msg_id IS NOT NULL
                          AND (m.created_at, m.rowid) < (o.created_at, o.rowid)
                   )
               )
             ORDER BY datetime(o.next_attempt_at) ASC, o.created_at ASC,
                      o.rowid ASC
             LIMIT ?
            """,
            (orjson.dumps(sorted(exclude)).decode(), int(limit)),
        )
        return [_row_to_entry(d) for d in rows_to_dicts(rows)]

    async def pending_multi_due(self, instance_id: str) -> str | None:
        """The latest ``next_attempt_at`` of *instance_id*'s pending rows of
        a multi payload (v_91, its ``msg_id`` set), or ``None`` when it has
        none — a live send to it must queue behind them, not overtake."""
        row = await self._db.fetchone(
            "SELECT MAX(datetime(o.next_attempt_at)) AS due"
            "  FROM federation_outbox o"
            "  JOIN federation_outbox_payloads p ON p.id = o.payload_id"
            " WHERE o.instance_id=? AND o.status='pending'"
            "   AND p.msg_id IS NOT NULL",
            (instance_id,),
        )
        return None if row is None else row["due"]

    async def ungroup_payload(self, payload_id: str) -> None:
        """Take the multi payload *payload_id* out of every group for good
        (the GFS refused its upload, or its frame cannot be built): its rows
        list with no ``msg_id`` and go one by one, while each per-row
        envelope still carries the stored ``msg_id``."""
        await self._db.enqueue(
            "UPDATE federation_outbox_payloads SET per_row=1 WHERE id=?",
            (payload_id,),
        )

    async def get_payload(self, payload_id: str) -> OutboxPayload | None:
        """The stored payload a ref row points at (migration 0145)."""
        row = await self._db.fetchone(
            "SELECT id, event_type, space_id, payload_enc, msg_id"
            " FROM federation_outbox_payloads WHERE id=?",
            (payload_id,),
        )
        if row is None:
            return None
        return OutboxPayload(
            id=row["id"],
            event_type=FederationEventType(row["event_type"]),
            space_id=row["space_id"],
            payload_enc=row["payload_enc"],
            msg_id=row["msg_id"],
        )

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
        A moment's delete we only relay for another household goes first
        (adversarial review F2): a household flooding deletes can crowd out
        other relayed ones, never our own events.
        """
        never = sorted(e.value for e in NEVER_DROP)
        placeholders = ",".join("?" * len(never))
        row = await self._db.fetchone(
            f"""
            SELECT id FROM federation_outbox
             WHERE instance_id=? AND status='pending'
               AND event_type NOT IN ({placeholders})
             ORDER BY COALESCE(substr(supersede_key, 1, ?) = ?, 0) DESC,
                      datetime(created_at) ASC, id ASC
             LIMIT 1
            """,
            (
                instance_id,
                *never,
                len(ANY_RELAYED_DELETE_KEY_PREFIX),
                ANY_RELAYED_DELETE_KEY_PREFIX,
            ),
        )
        if row is None:
            return False
        await self._db.enqueue(
            "DELETE FROM federation_outbox WHERE id=?",
            (row["id"],),
        )
        return True

    async def delete_for_instance(self, instance_id: str) -> None:
        """Delete every outbox row (any status) addressed to ``instance_id``.

        Called when the pairing is torn down: redelivery needs the peer's
        ``remote_instances`` row, so rows for an unpaired peer can never be
        delivered — NEVER_DROP included.
        """
        await self._db.enqueue(
            "DELETE FROM federation_outbox WHERE instance_id=?",
            (instance_id,),
        )

    async def delete_pending_of_types(
        self,
        instance_id: str,
        event_types: frozenset[FederationEventType],
    ) -> int:
        """Delete the PENDING rows of *event_types* addressed to
        ``instance_id``; return how many.

        For state a newer send supersedes: an envelope is sealed when it is
        queued, so a queued one would otherwise land after — and undo — a
        later send that was built under a stricter rule (§CP.F2).
        """
        if not event_types:
            return 0
        types = sorted(e.value for e in event_types)
        placeholders = ",".join("?" * len(types))
        return await self._db.enqueue_rowcount(
            f"DELETE FROM federation_outbox WHERE instance_id=?"
            f" AND status='pending' AND event_type IN ({placeholders})",
            (instance_id, *types),
        )

    async def purge_orphaned(self) -> int:
        """Delete every row (any status) addressed to a household we hold no
        ``remote_instances`` row for; return how many.

        :meth:`delete_for_instance` runs right after the row delete on every
        teardown path; this is the backstop for a crash between the two.
        Such a row can never be delivered (redelivery needs the row), so it
        is dropped outright instead of lingering as ``failed``. An unpair
        tombstone still HAS its row, so its queued ``UNPAIR`` stays.
        """
        return await self._db.enqueue_rowcount(
            "DELETE FROM federation_outbox WHERE NOT EXISTS ("
            "SELECT 1 FROM remote_instances"
            " WHERE remote_instances.id = federation_outbox.instance_id)",
        )

    async def expedite(
        self,
        instance_id: str,
        event_type: FederationEventType,
    ) -> bool:
        """Make ``instance_id``'s pending ``event_type`` rows due now.

        Used when the peer has just proven it is back (it sent us an
        envelope) so a row parked on a long backoff goes out on the next
        drain tick instead of hours later. Attempt counts are untouched —
        a failure still backs off from where it was.

        Debounced: only rows parked in the *future* are touched, and the
        write is skipped entirely when there are none. The caller is the
        inbound pipeline, so a sender repeating envelopes must cost one
        read each, not a queued write. Returns whether a row moved.
        """
        parked = await self._db.fetchone(
            "SELECT 1 FROM federation_outbox "
            "WHERE instance_id=? AND event_type=? AND status='pending' "
            "AND datetime(next_attempt_at) > datetime('now') LIMIT 1",
            (instance_id, event_type.value),
        )
        if parked is None:
            return False
        await self._db.enqueue(
            "UPDATE federation_outbox SET next_attempt_at=? "
            "WHERE instance_id=? AND event_type=? AND status='pending' "
            "AND datetime(next_attempt_at) > datetime('now')",
            (datetime.now(UTC).isoformat(), instance_id, event_type.value),
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
        supersede_key=row.get("supersede_key"),
        payload_id=row.get("payload_id"),
        space_id=row.get("payload_space_id"),
        msg_id=row.get("payload_msg_id"),
    )


#: One outbox row, inserted only for a household we still hold (and for an
#: unpair tombstone only its ``UNPAIR``) — see :meth:`SqliteOutboxRepo.enqueue`.
_INSERT_ROW = """
    INSERT INTO federation_outbox(
        id, instance_id, event_type, payload_json,
        authority_json, expires_at, supersede_key, payload_id
    )
    SELECT ?,?,?,?,?,?,?,?
     WHERE EXISTS (
        SELECT 1 FROM remote_instances
         WHERE id=? AND (status<>? OR ?=?)
     )
"""


def _row_params(
    entry_id: str,
    instance_id: str,
    event_type: FederationEventType,
    payload_json: str,
    authority_json: str | None,
    expires_at: str | None,
    supersede_key: str | None,
    payload_ref: OutboxPayload | None,
) -> tuple:
    """The parameters of one :data:`_INSERT_ROW`."""
    return (
        entry_id,
        instance_id,
        event_type.value,
        payload_json,
        authority_json,
        expires_at,
        supersede_key,
        payload_ref.id if payload_ref is not None else None,
        instance_id,
        PairingStatus.UNPAIRING.value,
        event_type.value,
        FederationEventType.UNPAIR.value,
    )


def _insert_refs(
    conn: sqlite3.Connection, payload: OutboxPayload, rows: list[tuple]
) -> list[tuple]:
    """Store *payload* (once) and its ref *rows* in one transaction; return
    the rows written. A payload no row references is dropped again."""
    conn.execute(
        "INSERT OR IGNORE INTO federation_outbox_payloads"
        "(id, event_type, space_id, payload_enc, msg_id) VALUES(?,?,?,?,?)",
        (
            payload.id,
            payload.event_type.value,
            payload.space_id,
            payload.payload_enc,
            payload.msg_id,
        ),
    )
    written = [params for params in rows if conn.execute(_INSERT_ROW, params).rowcount]
    if not written:
        conn.execute(
            "DELETE FROM federation_outbox_payloads WHERE id=? AND NOT EXISTS"
            " (SELECT 1 FROM federation_outbox WHERE payload_id=?)",
            (payload.id, payload.id),
        )
    return written
