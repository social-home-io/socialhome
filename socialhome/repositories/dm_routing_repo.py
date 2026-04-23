"""DM-relay routing repository (§12.5).

Wraps the SQL surface used by :class:`DmRoutingService` so the service
depends only on the abstract protocol — never on raw SQL or the
SQLite implementation.

Tables touched:

* ``network_discovery`` — peer-of-peer announcements from
  ``NETWORK_SYNC`` events.
* ``conversation_relay_paths`` — sticky per-(conv, target) primary
  path plus fallbacks.
* ``dm_relay_seen`` — 1-hour dedup ring.
* ``conversation_sender_sequences`` — per-(conv, sender) monotonic seq.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Protocol, runtime_checkable

from ..db import AsyncDatabase


@runtime_checkable
class AbstractDmRoutingRepo(Protocol):
    async def list_known_peers(
        self,
        source_instance_id: str,
    ) -> list[str]: ...

    async def upsert_network_discovery(
        self,
        *,
        peer_instance_id: str,
        discovered_via: str,
        seen_at: str,
        hop_count: int,
    ) -> None: ...

    async def upsert_conversation_path(
        self,
        *,
        conversation_id: str,
        target_instance: str,
        relay_via: str,
        hop_count: int,
        last_used_at: str,
    ) -> None: ...

    async def mark_seen(self, message_id: str) -> None: ...
    async def has_seen(self, message_id: str) -> bool: ...
    async def prune_seen(self, *, cutoff_iso: str) -> int: ...

    async def next_sender_seq(
        self,
        *,
        conversation_id: str,
        sender_user_id: str,
    ) -> int: ...

    async def peek_sender_seq(
        self,
        *,
        conversation_id: str,
        sender_user_id: str,
    ) -> int: ...

    # Gap detection -------------------------------------------------------
    async def insert_gaps(
        self,
        *,
        conversation_id: str,
        sender_user_id: str,
        expected_seqs: list[int],
    ) -> None: ...
    async def list_open_gaps(
        self,
        conversation_id: str,
    ) -> list[dict]: ...
    async def resolve_gap(
        self,
        *,
        conversation_id: str,
        sender_user_id: str,
        expected_seq: int,
    ) -> None: ...

    # Relay-path diagnostics (service-layer path selection read) ----------
    async def list_relay_paths(
        self,
        conversation_id: str,
    ) -> list[dict]: ...


class SqliteDmRoutingRepo:
    """SQLite-backed :class:`AbstractDmRoutingRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── network_discovery ──────────────────────────────────────────────

    async def list_known_peers(
        self,
        source_instance_id: str,
    ) -> list[str]:
        rows = await self._db.fetchall(
            "SELECT instance_id FROM network_discovery WHERE discovered_via=?",
            (source_instance_id,),
        )
        return [r["instance_id"] for r in rows]

    async def upsert_network_discovery(
        self,
        *,
        peer_instance_id: str,
        discovered_via: str,
        seen_at: str,
        hop_count: int,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO network_discovery(instance_id, discovered_via, seen_at, hop_count)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(instance_id, discovered_via) DO UPDATE SET
                seen_at=excluded.seen_at,
                hop_count=excluded.hop_count
            """,
            (peer_instance_id, discovered_via, seen_at, hop_count),
        )

    # ── conversation_relay_paths ───────────────────────────────────────

    async def upsert_conversation_path(
        self,
        *,
        conversation_id: str,
        target_instance: str,
        relay_via: str,
        hop_count: int,
        last_used_at: str,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO conversation_relay_paths(
                conversation_id, target_instance, relay_via, hop_count, last_used_at
            ) VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id, target_instance) DO UPDATE SET
                relay_via=excluded.relay_via,
                hop_count=excluded.hop_count,
                last_used_at=excluded.last_used_at
            """,
            (
                conversation_id,
                target_instance,
                relay_via,
                hop_count,
                last_used_at,
            ),
        )

    # ── dedup ring ─────────────────────────────────────────────────────

    async def mark_seen(self, message_id: str) -> None:
        await self._db.enqueue(
            "INSERT OR IGNORE INTO dm_relay_seen(msg_id) VALUES(?)",
            (message_id,),
        )

    async def has_seen(self, message_id: str) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM dm_relay_seen WHERE msg_id=?",
            (message_id,),
        )
        return row is not None

    async def prune_seen(self, *, cutoff_iso: str) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM dm_relay_seen WHERE seen_at < ?",
            (cutoff_iso,),
        )
        n = int(row["n"]) if row else 0
        if n:
            await self._db.enqueue(
                "DELETE FROM dm_relay_seen WHERE seen_at < ?",
                (cutoff_iso,),
            )
        return n

    # ── sender sequence ────────────────────────────────────────────────

    async def next_sender_seq(
        self,
        *,
        conversation_id: str,
        sender_user_id: str,
    ) -> int:
        """Atomically increment + return the next sender_seq."""

        def _run(conn):
            conn.execute(
                """
                INSERT INTO conversation_sender_sequences(
                    conversation_id, sender_user_id, last_seq
                ) VALUES(?, ?, 1)
                ON CONFLICT(conversation_id, sender_user_id) DO UPDATE SET
                    last_seq = last_seq + 1
                """,
                (conversation_id, sender_user_id),
            )
            row = conn.execute(
                "SELECT last_seq FROM conversation_sender_sequences"
                " WHERE conversation_id=? AND sender_user_id=?",
                (conversation_id, sender_user_id),
            ).fetchone()
            return int(row[0]) if row else 1

        return await self._db.transact(_run)

    async def peek_sender_seq(
        self,
        *,
        conversation_id: str,
        sender_user_id: str,
    ) -> int:
        """Return the last-seen seq for (conv, sender) without incrementing."""
        row = await self._db.fetchone(
            "SELECT last_seq FROM conversation_sender_sequences "
            "WHERE conversation_id=? AND sender_user_id=?",
            (conversation_id, sender_user_id),
        )
        return int(row["last_seq"]) if row else 0

    # ── Gap detection ──────────────────────────────────────────────────

    async def insert_gaps(
        self,
        *,
        conversation_id: str,
        sender_user_id: str,
        expected_seqs: list[int],
    ) -> None:
        """Record one row per missing sequence number.

        ``conversation_message_gaps`` PK is
        (conv, sender, expected_seq) so re-detecting the same hole
        twice is idempotent.
        """
        for seq in expected_seqs:
            await self._db.enqueue(
                """
                INSERT OR IGNORE INTO conversation_message_gaps(
                    conversation_id, sender_user_id, expected_seq
                ) VALUES(?, ?, ?)
                """,
                (conversation_id, sender_user_id, int(seq)),
            )

    async def list_open_gaps(
        self,
        conversation_id: str,
    ) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT sender_user_id, expected_seq, detected_at "
            "FROM conversation_message_gaps WHERE conversation_id=? "
            "ORDER BY detected_at, expected_seq",
            (conversation_id,),
        )
        return [
            {
                "sender_user_id": r["sender_user_id"],
                "expected_seq": int(r["expected_seq"]),
                "detected_at": r["detected_at"],
            }
            for r in rows
        ]

    async def resolve_gap(
        self,
        *,
        conversation_id: str,
        sender_user_id: str,
        expected_seq: int,
    ) -> None:
        await self._db.enqueue(
            "DELETE FROM conversation_message_gaps "
            "WHERE conversation_id=? AND sender_user_id=? AND expected_seq=?",
            (conversation_id, sender_user_id, int(expected_seq)),
        )

    # ── Relay-path diagnostics ─────────────────────────────────────────

    async def list_relay_paths(
        self,
        conversation_id: str,
    ) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT target_instance, relay_via, hop_count, last_used_at "
            "FROM conversation_relay_paths WHERE conversation_id=? "
            "ORDER BY hop_count, last_used_at DESC",
            (conversation_id,),
        )
        return [
            {
                "target_instance": r["target_instance"],
                "relay_via": r["relay_via"],
                "hop_count": int(r["hop_count"]),
                "last_used_at": r["last_used_at"],
            }
            for r in rows
        ]


def utcnow_iso() -> str:
    """Helper used by callers that need the same timestamp the repo uses."""
    return datetime.now(timezone.utc).isoformat()


def normalize_peers(peer_ids: Iterable[str], *, cap: int = 50) -> list[str]:
    """De-dupe + cap the peer list before persisting NETWORK_SYNC rows.

    Caps malicious graph inflation (S-17). Service code calls this to
    pre-filter the iterable before looping over it; the repo stays
    side-effect free per peer so the service can pass `cap` once.
    """
    out: list[str] = []
    seen: set[str] = set()
    for pid in peer_ids:
        if not isinstance(pid, str) or not pid or pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
        if len(out) >= cap:
            break
    return out
