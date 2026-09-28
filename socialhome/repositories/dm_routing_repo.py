"""DM-relay routing repository (§12.5).

Wraps the SQL surface used by :class:`DmRoutingService` so the service
depends only on the abstract protocol — never on raw SQL or the
SQLite implementation.

Tables touched:

* ``network_discovery`` — peer-of-peer announcements from
  ``NETWORK_SYNC`` events.
* ``conversation_relay_paths`` — sticky per-(conv, sender) primary
  path plus alternatives (spec §18587).
* ``dm_relay_seen`` — 1-hour dedup ring.
* ``conversation_sender_sequences`` — per-(conv, sender) monotonic seq.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Protocol, runtime_checkable

import orjson

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

    async def set_relay_paths(
        self,
        *,
        conversation_id: str,
        sender_user_id: str,
        target_instance: str,
        primary: list[str],
        alternatives: list[list[str]],
    ) -> None: ...

    async def get_relay_paths(
        self,
        conversation_id: str,
        sender_user_id: str,
    ) -> dict | None: ...

    async def clear_relay_paths(
        self,
        conversation_id: str,
        sender_user_id: str | None = None,
    ) -> None: ...

    async def forget_discovered_via(self, source_instance_id: str) -> None: ...

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

    async def record_received_seq(
        self,
        *,
        conversation_id: str,
        sender_user_id: str,
        seq: int,
    ) -> None: ...

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

    async def clear_all_gaps_for_sender(
        self,
        *,
        conversation_id: str,
        sender_user_id: str,
    ) -> None: ...

    # Relay-path diagnostics (service-layer path selection read) ----------
    async def list_relay_paths(
        self,
        conversation_id: str,
    ) -> list[dict]: ...

    # last_relay_for helper -----------------------------------------------
    async def fetch_last_relay_for(
        self,
        target_instance_id: str,
        *,
        cutoff_iso: str,
    ) -> dict | None: ...


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

    async def forget_discovered_via(self, source_instance_id: str) -> None:
        """Drop every peer ``source_instance_id`` announced to us.

        Called on unpair: a household we are no longer paired with is no
        longer a trusted source of mesh topology, so its ``NETWORK_SYNC``
        announcements must stop feeding path selection.
        """
        await self._db.enqueue(
            "DELETE FROM network_discovery WHERE discovered_via=?",
            (source_instance_id,),
        )

    # ── conversation_relay_paths ───────────────────────────────────────

    async def set_relay_paths(
        self,
        *,
        conversation_id: str,
        sender_user_id: str,
        target_instance: str,
        primary: list[str],
        alternatives: list[list[str]],
    ) -> None:
        """Replace the stored paths for (conversation, sender).

        Wipes prior rows and writes ``primary`` at ``path_index=0``,
        then alternatives at ``1+`` in the order given. The full path
        (each hop instance_id) is stored as JSON so
        :meth:`get_or_select_path` can validate every hop, not just the
        first one.
        """

        def _run(conn):
            conn.execute(
                "DELETE FROM conversation_relay_paths "
                "WHERE conversation_id=? AND sender_user_id=?",
                (conversation_id, sender_user_id),
            )
            now = utcnow_iso()
            rows: list[tuple] = []
            paths = [primary, *alternatives]
            for idx, path in enumerate(paths):
                if not path:
                    continue
                rows.append(
                    (
                        conversation_id,
                        sender_user_id,
                        idx,
                        target_instance,
                        orjson.dumps(path).decode(),
                        len(path),
                        now,
                    ),
                )
            if rows:
                conn.executemany(
                    """
                    INSERT INTO conversation_relay_paths(
                        conversation_id, sender_user_id, path_index,
                        target_instance, relay_path, hop_count, last_used_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

        await self._db.transact(_run)

    async def get_relay_paths(
        self,
        conversation_id: str,
        sender_user_id: str,
    ) -> dict | None:
        """Return the stored ``{primary, alternatives, target_instance}``
        for (conversation, sender), or ``None`` if no paths are stored.
        """
        rows = await self._db.fetchall(
            "SELECT path_index, target_instance, relay_path "
            "FROM conversation_relay_paths "
            "WHERE conversation_id=? AND sender_user_id=? "
            "ORDER BY path_index",
            (conversation_id, sender_user_id),
        )
        if not rows:
            return None
        primary: list[str] = []
        alternatives: list[list[str]] = []
        target_instance = ""
        for r in rows:
            try:
                path = orjson.loads(r["relay_path"])
            except TypeError, ValueError:
                continue
            if not isinstance(path, list):
                continue
            target_instance = target_instance or r["target_instance"]
            if int(r["path_index"]) == 0:
                primary = [str(h) for h in path]
            else:
                alternatives.append([str(h) for h in path])
        if not primary and not alternatives:
            return None
        return {
            "primary": primary,
            "alternatives": alternatives,
            "target_instance": target_instance,
        }

    async def clear_relay_paths(
        self,
        conversation_id: str,
        sender_user_id: str | None = None,
    ) -> None:
        """Drop all stored paths for (conversation, sender).

        ``sender_user_id=None`` clears every sender's paths for the
        conversation — used when a conversation is deleted.
        """
        if sender_user_id is None:
            await self._db.enqueue(
                "DELETE FROM conversation_relay_paths WHERE conversation_id=?",
                (conversation_id,),
            )
        else:
            await self._db.enqueue(
                "DELETE FROM conversation_relay_paths "
                "WHERE conversation_id=? AND sender_user_id=?",
                (conversation_id, sender_user_id),
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
        # ``mark_seen`` writes the column DEFAULT (``datetime('now')``,
        # space-separator, no TZ). The scheduler hands us an ISO-with-``T``
        # cutoff. A bare lexical compare would treat
        # ``'2026-05-10 16:30:00'`` as *less than*
        # ``'2026-05-10T15:30:00+00:00'`` (space 0x20 < 'T' 0x54) and
        # prune fresh rows. Wrapping both sides in ``datetime(...)``
        # normalises them — same trick ``outbox_repo`` uses.
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM dm_relay_seen "
            "WHERE datetime(seen_at) < datetime(?)",
            (cutoff_iso,),
        )
        n = int(row["n"]) if row else 0
        if n:
            await self._db.enqueue(
                "DELETE FROM dm_relay_seen WHERE datetime(seen_at) < datetime(?)",
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

    async def record_received_seq(
        self,
        *,
        conversation_id: str,
        sender_user_id: str,
        seq: int,
    ) -> None:
        """Advance the (conv, sender) high-watermark to ``seq``.

        Called by the inbound DM handler after each forward (non-replay)
        envelope. Without this, ``peek_sender_seq`` stays at 0 forever on
        the receiver side and every message after the first trips a
        false ``missing=1..N-1`` gap warning. The ``MAX`` clause makes
        the call safe for late-arriving out-of-order envelopes — they
        don't rewind the watermark.

        Note: this writes a row keyed on (conv, sender=peer). The
        ``next_sender_seq`` path writes its own row keyed on
        (conv, sender=me), so the two callers never contend on the
        same row.
        """
        await self._db.enqueue(
            """
            INSERT INTO conversation_sender_sequences(
                conversation_id, sender_user_id, last_seq
            ) VALUES(?, ?, ?)
            ON CONFLICT(conversation_id, sender_user_id) DO UPDATE SET
                last_seq = MAX(last_seq, excluded.last_seq)
            """,
            (conversation_id, sender_user_id, int(seq)),
        )

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

    async def clear_all_gaps_for_sender(
        self,
        *,
        conversation_id: str,
        sender_user_id: str,
    ) -> None:
        """Drop every open-gap row for ``(conversation_id, sender_user_id)``.

        Used to clean up the false-positive rows the pre-fix gap detector
        inserted while the receiver's high-watermark never advanced past 0.
        Safe to call only when the caller has confirmed the prior watermark
        WAS 0 — at that point any existing gap row is by construction
        bogus (you cannot legitimately track "I expected seq=X but it
        never came" before you've ever received anything).
        """
        await self._db.enqueue(
            "DELETE FROM conversation_message_gaps "
            "WHERE conversation_id=? AND sender_user_id=?",
            (conversation_id, sender_user_id),
        )

    # ── Relay-path diagnostics ─────────────────────────────────────────

    async def list_relay_paths(
        self,
        conversation_id: str,
    ) -> list[dict]:
        """All stored paths for a conversation, primary-first.

        Diagnostic view: returns one dict per row (across all senders +
        path_index values) so admin tooling can inspect the full
        ``conversation_relay_paths`` state.
        """
        rows = await self._db.fetchall(
            "SELECT sender_user_id, path_index, target_instance, "
            "relay_path, hop_count, last_used_at "
            "FROM conversation_relay_paths WHERE conversation_id=? "
            "ORDER BY sender_user_id, path_index",
            (conversation_id,),
        )
        out: list[dict] = []
        for r in rows:
            try:
                path = orjson.loads(r["relay_path"])
            except TypeError, ValueError:
                path = []
            out.append(
                {
                    "sender_user_id": r["sender_user_id"],
                    "path_index": int(r["path_index"]),
                    "target_instance": r["target_instance"],
                    "relay_path": path,
                    "relay_via": path[0] if path else "",
                    "hop_count": int(r["hop_count"]),
                    "last_used_at": r["last_used_at"],
                }
            )
        return out

    async def fetch_last_relay_for(
        self,
        target_instance_id: str,
        *,
        cutoff_iso: str,
    ) -> dict | None:
        """Return the most recent multi-hop relay path record targeting
        ``target_instance_id`` with ``last_used_at >= cutoff_iso``.

        Only considers primary paths (``path_index = 0``) that have more
        than one hop (``hop_count > 1``), so a direct single-hop send is
        not mistaken for a relayed DM.

        Uses ``datetime(...)`` on both sides of the comparison so that the
        SQLite-default space-separator format and the Python ISO-with-T
        format sort consistently — same technique as ``prune_seen``.
        """
        row = await self._db.fetchone(
            "SELECT relay_path, last_used_at "
            "FROM conversation_relay_paths "
            "WHERE target_instance=? AND path_index=0 AND hop_count>1 "
            "  AND datetime(last_used_at) >= datetime(?) "
            "ORDER BY datetime(last_used_at) DESC LIMIT 1",
            (target_instance_id, cutoff_iso),
        )
        if row is None:
            return None
        return {"relay_path": row["relay_path"], "last_used_at": row["last_used_at"]}

    async def insert_relay_path_for_test(
        self,
        *,
        conversation_id: str,
        sender_user_id: str,
        target_instance: str,
        via: str,
        ts: str,
    ) -> None:
        """Test seeding helper — deliberately NOT on the Protocol contract.

        Direct INSERT into ``conversation_relay_paths`` for unit-test
        fixtures. Production callers should use the regular
        ``select_conversation_path`` / ``get_or_select_path`` write path.

        Avoids the full BFS + conversation machinery so unit tests can
        control the ``target_instance``, ``via`` (first hop), and
        ``last_used_at`` timestamp independently.
        """
        relay_path_json = orjson.dumps([via, target_instance]).decode()
        await self._db.enqueue(
            "INSERT OR REPLACE INTO conversation_relay_paths("
            "  conversation_id, sender_user_id, path_index,"
            "  target_instance, relay_path, hop_count, last_used_at"
            ") VALUES(?, ?, 0, ?, ?, 2, ?)",
            (conversation_id, sender_user_id, target_instance, relay_path_json, ts),
        )


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
