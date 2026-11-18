"""GFS connection repository — wraps gfs_connections + gfs_space_publications."""

from __future__ import annotations

import sqlite3
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.federation import GfsConnection, GfsSpacePublication
from ..infrastructure.key_manager import KeyManagerError
from .base import rows_to_dicts


@runtime_checkable
class AbstractGfsConnectionRepo(Protocol):
    """Interface for GFS connection persistence."""

    async def save(self, conn: GfsConnection) -> None: ...
    async def get(self, gfs_id: str) -> GfsConnection | None: ...
    async def list_active(self) -> list[GfsConnection]: ...
    async def list_all(self) -> list[GfsConnection]: ...
    async def update_status(self, gfs_id: str, status: str) -> None: ...
    async def update_display_name(self, gfs_id: str, display_name: str) -> None: ...
    async def update_gfs_instance_id(
        self, gfs_id: str, gfs_instance_id: str
    ) -> bool: ...
    async def mark_proof_required(self, gfs_id: str, at: str) -> bool: ...
    async def delete(self, gfs_id: str) -> None: ...
    async def publish_space(
        self, space_id: str, gfs_id: str, status: str = "active"
    ) -> GfsSpacePublication: ...
    async def unpublish_space(self, space_id: str, gfs_id: str) -> None: ...
    async def list_publications(self, gfs_id: str) -> list[GfsSpacePublication]: ...
    async def list_publications_for_space(
        self, space_id: str
    ) -> list[GfsSpacePublication]: ...
    async def list_gfs_for_space(self, space_id: str) -> list[GfsConnection]: ...
    async def list_active_listings_for_space(
        self, space_id: str
    ) -> list[GfsConnection]: ...
    async def count_published_spaces(self, gfs_id: str) -> int: ...
    async def list_publications_all(self) -> list[dict]: ...
    async def add_channel_withdrawals(
        self,
        space_id: str,
        channel_id: str,
        gfs_ids: list[str],
        *,
        channel_seed: bytes | None = None,
    ) -> None: ...
    async def get_channel_withdrawal_seed(
        self, space_id: str, gfs_id: str, channel_id: str
    ) -> bytes | None: ...
    async def list_channel_withdrawals(
        self, *, space_id: str | None = None, gfs_id: str | None = None
    ) -> list[tuple[str, str, str]]: ...
    async def delete_channel_withdrawal(
        self, space_id: str, gfs_id: str, channel_id: str
    ) -> None: ...
    async def mark_listing_withdrawn(
        self, space_id: str, gfs_id: str, pinned_key: str | None
    ) -> None: ...
    async def list_withdrawn_listings(
        self, *, space_id: str | None = None, gfs_id: str | None = None
    ) -> list[tuple[str, str, str | None]]: ...
    async def set_withdrawn_listing_pin(
        self, space_id: str, gfs_id: str, pinned_key: str
    ) -> None: ...
    async def delete_withdrawn_listing(self, space_id: str, gfs_id: str) -> None: ...


class SqliteGfsConnectionRepo:
    """SQLite-backed :class:`AbstractGfsConnectionRepo`.

    ``key_manager`` is the household KEK that wraps a retired channel's
    signing seed at rest (``gfs_channel_withdrawals.channel_seed``). Like
    the space repo's, it is loaded after the repos are built and wired with
    :meth:`attach_key_manager`; only the seed accessors need it.
    """

    __slots__ = ("_db", "_kek")

    def __init__(self, db: AsyncDatabase, *, key_manager=None) -> None:
        self._db = db
        self._kek = key_manager

    def attach_key_manager(self, key_manager) -> None:
        """Wire the household KEK after construction (see class docstring)."""
        self._kek = key_manager

    async def save(self, conn: GfsConnection) -> None:
        await self._db.enqueue(
            """
            INSERT INTO gfs_connections(
                id, gfs_instance_id, display_name, public_key,
                inbox_url, status, paired_at, proof_required_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                gfs_instance_id=excluded.gfs_instance_id,
                display_name=excluded.display_name,
                public_key=excluded.public_key,
                inbox_url=excluded.inbox_url,
                status=excluded.status,
                paired_at=excluded.paired_at,
                -- The proof ratchet only ever gets set, never cleared.
                proof_required_at=COALESCE(
                    gfs_connections.proof_required_at,
                    excluded.proof_required_at
                )
            """,
            (
                conn.id,
                conn.gfs_instance_id,
                conn.display_name,
                conn.public_key,
                conn.inbox_url,
                conn.status,
                conn.paired_at,
                conn.proof_required_at,
            ),
        )

    async def get(self, gfs_id: str) -> GfsConnection | None:
        row = await self._db.fetchone(
            "SELECT * FROM gfs_connections WHERE id=?",
            (gfs_id,),
        )
        if row is None:
            return None
        return _row_to_conn(dict(zip(row.keys(), tuple(row))))

    async def list_active(self) -> list[GfsConnection]:
        rows = await self._db.fetchall(
            "SELECT * FROM gfs_connections WHERE status='active'"
            " ORDER BY paired_at DESC",
        )
        return [_row_to_conn(r) for r in rows_to_dicts(rows)]

    async def list_all(self) -> list[GfsConnection]:
        rows = await self._db.fetchall(
            "SELECT * FROM gfs_connections ORDER BY paired_at DESC",
        )
        return [_row_to_conn(r) for r in rows_to_dicts(rows)]

    async def update_status(self, gfs_id: str, status: str) -> None:
        await self._db.enqueue(
            "UPDATE gfs_connections SET status=? WHERE id=?",
            (status, gfs_id),
        )

    async def update_display_name(self, gfs_id: str, display_name: str) -> None:
        await self._db.enqueue(
            "UPDATE gfs_connections SET display_name=? WHERE id=?",
            (display_name, gfs_id),
        )

    async def update_gfs_instance_id(self, gfs_id: str, gfs_instance_id: str) -> bool:
        """Rebind connection *gfs_id* to the server's new public id.

        ``gfs_instance_id`` is UNIQUE. Returns ``False`` — and writes
        nothing — when ANOTHER connection already holds *gfs_instance_id*
        (the same server paired twice); ``True`` otherwise.
        """
        row = await self._db.fetchone(
            "SELECT id FROM gfs_connections WHERE gfs_instance_id=? AND id<>?",
            (gfs_instance_id, gfs_id),
        )
        if row is not None:
            return False
        try:
            await self._db.enqueue(
                "UPDATE gfs_connections SET gfs_instance_id=? WHERE id=?",
                (gfs_instance_id, gfs_id),
            )
        except sqlite3.IntegrityError:
            # Another connection took the id between the read and the write.
            return False
        return True

    async def mark_proof_required(self, gfs_id: str, at: str) -> bool:
        """Set the proof-of-possession ratchet (0094) the first time; returns
        whether this call set it. Never moves or clears an existing stamp."""
        row = await self._db.fetchone(
            "SELECT proof_required_at FROM gfs_connections WHERE id=?",
            (gfs_id,),
        )
        if row is None or row["proof_required_at"] is not None:
            return False
        await self._db.enqueue(
            "UPDATE gfs_connections SET proof_required_at=?"
            " WHERE id=? AND proof_required_at IS NULL",
            (at, gfs_id),
        )
        return True

    async def delete(self, gfs_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM gfs_space_publications WHERE gfs_connection_id=?",
            (gfs_id,),
        )
        await self._db.enqueue(
            "DELETE FROM gfs_channel_withdrawals WHERE gfs_connection_id=?",
            (gfs_id,),
        )
        await self._db.enqueue(
            "DELETE FROM gfs_withdrawn_listings WHERE gfs_connection_id=?",
            (gfs_id,),
        )
        await self._db.enqueue(
            "DELETE FROM gfs_connections WHERE id=?",
            (gfs_id,),
        )

    async def publish_space(
        self, space_id: str, gfs_id: str, status: str = "active"
    ) -> GfsSpacePublication:
        # Upsert so a re-publish refreshes the GFS-returned status
        # (e.g. pending → active once a moderator approves).
        await self._db.enqueue(
            "INSERT INTO gfs_space_publications(space_id, gfs_connection_id, status)"
            " VALUES(?, ?, ?)"
            " ON CONFLICT(space_id, gfs_connection_id) DO UPDATE SET"
            " status=excluded.status",
            (space_id, gfs_id, status),
        )
        # Listed there again: it is no longer a withdrawn listing (0147).
        await self._db.enqueue(
            "DELETE FROM gfs_withdrawn_listings"
            " WHERE space_id=? AND gfs_connection_id=?",
            (space_id, gfs_id),
        )
        # Fetch the row back so ``published_at`` (DB ``datetime('now')``
        # default) is authoritative rather than synthesised. ``enqueue``
        # awaits the write's completion, so the row is visible here.
        row = await self._db.fetchone(
            "SELECT * FROM gfs_space_publications"
            " WHERE space_id=? AND gfs_connection_id=?",
            (space_id, gfs_id),
        )
        assert row is not None  # just written
        return _row_to_publication(rows_to_dicts([row])[0])

    async def unpublish_space(self, space_id: str, gfs_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM gfs_space_publications"
            " WHERE space_id=? AND gfs_connection_id=?",
            (space_id, gfs_id),
        )

    async def list_publications(self, gfs_id: str) -> list[GfsSpacePublication]:
        rows = await self._db.fetchall(
            "SELECT * FROM gfs_space_publications WHERE gfs_connection_id=?"
            " ORDER BY published_at DESC",
            (gfs_id,),
        )
        return [_row_to_publication(r) for r in rows_to_dicts(rows)]

    async def list_publications_for_space(
        self, space_id: str
    ) -> list[GfsSpacePublication]:
        rows = await self._db.fetchall(
            "SELECT * FROM gfs_space_publications WHERE space_id=?"
            " ORDER BY published_at DESC",
            (space_id,),
        )
        return [_row_to_publication(r) for r in rows_to_dicts(rows)]

    async def list_gfs_for_space(self, space_id: str) -> list[GfsConnection]:
        rows = await self._db.fetchall(
            "SELECT gc.* FROM gfs_connections gc"
            " INNER JOIN gfs_space_publications gsp"
            "   ON gc.id = gsp.gfs_connection_id"
            " WHERE gsp.space_id=?"
            " ORDER BY gc.paired_at DESC",
            (space_id,),
        )
        return [_row_to_conn(r) for r in rows_to_dicts(rows)]

    async def list_active_listings_for_space(
        self, space_id: str
    ) -> list[GfsConnection]:
        """The ACTIVE connections on which *space_id* is ACTIVELY listed
        (publication ``status='active'`` — not pending review, not banned),
        ordered by ``gfs_instance_id`` so an unchanged set reads back in the
        same order. The owner's ``spaces.gfs_servers`` is derived from it."""
        rows = await self._db.fetchall(
            "SELECT gc.* FROM gfs_connections gc"
            " INNER JOIN gfs_space_publications gsp"
            "   ON gc.id = gsp.gfs_connection_id"
            " WHERE gsp.space_id=? AND gsp.status='active'"
            "   AND gc.status='active'"
            " ORDER BY gc.gfs_instance_id, gc.id",
            (space_id,),
        )
        return [_row_to_conn(r) for r in rows_to_dicts(rows)]

    async def count_published_spaces(self, gfs_id: str) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM gfs_space_publications"
            " WHERE gfs_connection_id=?",
            (gfs_id,),
        )
        return int(row["n"]) if row else 0

    async def list_publications_all(self) -> list[dict]:
        """Aggregate every (space, gfs) publication with join-friendly
        metadata. Used by the admin Spaces tab to render the "currently
        published to" list without N+1 per-space fetches.
        """
        rows = await self._db.fetchall(
            """
            SELECT gsp.space_id            AS space_id,
                   gsp.gfs_connection_id   AS gfs_id,
                   gsp.published_at        AS published_at,
                   gsp.status              AS status,
                   gc.display_name         AS gfs_display_name,
                   gc.inbox_url         AS gfs_inbox_url,
                   s.name                  AS space_name,
                   s.emoji                 AS space_emoji
              FROM gfs_space_publications AS gsp
              JOIN gfs_connections AS gc ON gc.id = gsp.gfs_connection_id
              LEFT JOIN spaces AS s ON s.id = gsp.space_id
             ORDER BY gsp.published_at DESC
            """,
        )
        return rows_to_dicts(rows)

    # ── Pending channel withdrawals (migration 0095) ─────────────────────

    @staticmethod
    def _channel_aad(space_id: str, gfs_id: str, channel_id: str) -> bytes:
        return f"gfs-channel-withdrawal:{space_id}:{gfs_id}:{channel_id}".encode()

    async def add_channel_withdrawals(
        self,
        space_id: str,
        channel_id: str,
        gfs_ids: list[str],
        *,
        channel_seed: bytes | None = None,
    ) -> None:
        """Record that *channel_id* of *space_id* must still be unregistered
        on each connection in *gfs_ids* (idempotent; an id that is no
        connection any more is skipped).

        *channel_seed* (migration 0147) is the channel's 32-byte signing
        seed, stored KEK-wrapped and bound to its row, so the unregister can
        still be signed after the space key it derives from is rotated away.
        A row already recorded without one gains it."""
        if channel_seed is not None:
            if self._kek is None:
                raise RuntimeError("channel seed persistence requires a key_manager")
            if len(channel_seed) != 32:
                raise ValueError("channel seed must be 32 bytes")
        for gfs_id in dict.fromkeys(gfs_ids):
            wrapped = (
                self._kek.encrypt(
                    channel_seed,
                    associated_data=self._channel_aad(space_id, gfs_id, channel_id),
                )
                if channel_seed is not None and self._kek is not None
                else None
            )
            await self._db.enqueue(
                "INSERT INTO gfs_channel_withdrawals"
                "(space_id, gfs_connection_id, channel_id, channel_seed)"
                " SELECT ?, id, ?, ? FROM gfs_connections WHERE id=?"
                " ON CONFLICT(space_id, gfs_connection_id, channel_id)"
                " DO UPDATE SET channel_seed="
                "   COALESCE(excluded.channel_seed, channel_seed)",
                (space_id, channel_id, wrapped, gfs_id),
            )

    async def get_channel_withdrawal_seed(
        self, space_id: str, gfs_id: str, channel_id: str
    ) -> bytes | None:
        """The stored signing seed of a pending channel withdrawal, or
        ``None`` when there is no row or none was recorded with it.

        A seed that WAS recorded and cannot be read — no KEK wired, or a
        value that does not unwrap for this row — raises
        :class:`KeyManagerError`: "unreadable" must never be mistaken for
        "none", or the caller would give the unregister up."""
        row = await self._db.fetchone(
            "SELECT channel_seed FROM gfs_channel_withdrawals"
            " WHERE space_id=? AND gfs_connection_id=? AND channel_id=?",
            (space_id, gfs_id, channel_id),
        )
        if row is None or row["channel_seed"] is None:
            return None
        if self._kek is None:
            raise KeyManagerError("a channel seed is stored but no KEK is wired")
        return self._kek.decrypt(
            row["channel_seed"],
            associated_data=self._channel_aad(space_id, gfs_id, channel_id),
        )

    async def list_channel_withdrawals(
        self, *, space_id: str | None = None, gfs_id: str | None = None
    ) -> list[tuple[str, str, str]]:
        """Pending ``(space_id, gfs_connection_id, channel_id)``, oldest
        first, optionally for one space and / or one connection."""
        where: list[str] = []
        params: list[str] = []
        if space_id is not None:
            where.append("space_id=?")
            params.append(space_id)
        if gfs_id is not None:
            where.append("gfs_connection_id=?")
            params.append(gfs_id)
        sql = (
            "SELECT space_id, gfs_connection_id, channel_id"
            " FROM gfs_channel_withdrawals"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = await self._db.fetchall(
            sql + " ORDER BY created_at, rowid", tuple(params)
        )
        return [(r["space_id"], r["gfs_connection_id"], r["channel_id"]) for r in rows]

    async def delete_channel_withdrawal(
        self, space_id: str, gfs_id: str, channel_id: str
    ) -> None:
        await self._db.enqueue(
            "DELETE FROM gfs_channel_withdrawals"
            " WHERE space_id=? AND gfs_connection_id=? AND channel_id=?",
            (space_id, gfs_id, channel_id),
        )

    # ── Withdrawn listings a GFS still pins a key for (migration 0147) ───

    async def mark_listing_withdrawn(
        self, space_id: str, gfs_id: str, pinned_key: str | None
    ) -> None:
        """Record that *space_id*'s listing on *gfs_id* was withdrawn while
        the server keeps its row — *pinned_key* is the authority key it last
        confirmed (``None`` = unknown; a known one is never forgotten). An
        id that is no connection any more records nothing."""
        await self._db.enqueue(
            "INSERT INTO gfs_withdrawn_listings"
            "(space_id, gfs_connection_id, pinned_key)"
            " SELECT ?, id, ? FROM gfs_connections WHERE id=?"
            " ON CONFLICT(space_id, gfs_connection_id) DO UPDATE SET"
            " pinned_key=COALESCE(excluded.pinned_key, pinned_key)",
            (space_id, pinned_key, gfs_id),
        )

    async def list_withdrawn_listings(
        self, *, space_id: str | None = None, gfs_id: str | None = None
    ) -> list[tuple[str, str, str | None]]:
        """``(space_id, gfs_connection_id, pinned_key)`` of every withdrawn
        listing, oldest first, optionally for one space and / or one
        connection."""
        where: list[str] = []
        params: list[str] = []
        if space_id is not None:
            where.append("space_id=?")
            params.append(space_id)
        if gfs_id is not None:
            where.append("gfs_connection_id=?")
            params.append(gfs_id)
        sql = (
            "SELECT space_id, gfs_connection_id, pinned_key FROM gfs_withdrawn_listings"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = await self._db.fetchall(
            sql + " ORDER BY created_at, rowid", tuple(params)
        )
        return [(r["space_id"], r["gfs_connection_id"], r["pinned_key"]) for r in rows]

    async def set_withdrawn_listing_pin(
        self, space_id: str, gfs_id: str, pinned_key: str
    ) -> None:
        await self._db.enqueue(
            "UPDATE gfs_withdrawn_listings SET pinned_key=?"
            " WHERE space_id=? AND gfs_connection_id=?",
            (pinned_key, space_id, gfs_id),
        )

    async def delete_withdrawn_listing(self, space_id: str, gfs_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM gfs_withdrawn_listings"
            " WHERE space_id=? AND gfs_connection_id=?",
            (space_id, gfs_id),
        )


def _row_to_publication(r: dict) -> GfsSpacePublication:
    return GfsSpacePublication(
        space_id=r["space_id"],
        gfs_connection_id=r["gfs_connection_id"],
        published_at=r["published_at"],
        status=r["status"],
    )


def _row_to_conn(r: dict) -> GfsConnection:
    return GfsConnection(
        id=r["id"],
        gfs_instance_id=r["gfs_instance_id"],
        display_name=r["display_name"],
        public_key=r["public_key"],
        inbox_url=r["inbox_url"],
        status=r["status"],
        paired_at=r["paired_at"],
        created_at=r.get("created_at"),
        proof_required_at=r.get("proof_required_at"),
    )
