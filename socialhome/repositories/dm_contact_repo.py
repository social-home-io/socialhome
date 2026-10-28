"""DM contact-request repository (§23.47).

Pre-pairing handshake surface: when a user on a paired instance wants
to message a local user they haven't exchanged keys with yet, a
``DM_CONTACT_REQUEST`` federation event arrives and
:class:`PairingInboundHandlers` persists a pending row via this repo.
The admin UI then shows it as pending and the recipient can
accept/decline.

Kept deliberately tiny: :class:`DmContactService` answers a request
(``answer_pending`` — every pending request from one person to one
recipient, as one conditional write) and "Needs you" checks it
(``latest_between``).
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from .base import row_to_dict, rows_to_dicts


@runtime_checkable
class AbstractDmContactRepo(Protocol):
    async def save_request(
        self,
        *,
        from_user_id: str,
        to_user_id: str,
    ) -> str: ...
    async def list_pending_for(self, to_user_id: str) -> list[dict]: ...
    async def set_status(self, request_id: str, status: str) -> None: ...
    async def latest_between(
        self, from_user_id: str, to_user_id: str
    ) -> dict | None: ...
    async def answer_pending(
        self, from_user_id: str, to_user_id: str, status: str
    ) -> int: ...


class SqliteDmContactRepo:
    """SQLite-backed :class:`AbstractDmContactRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def save_request(
        self,
        *,
        from_user_id: str,
        to_user_id: str,
    ) -> str:
        """Insert a new pending request. Returns the generated request id."""
        request_id = uuid.uuid4().hex
        await self._db.enqueue(
            """
            INSERT INTO dm_contact_requests(
                id, from_user_id, to_user_id, status
            ) VALUES(?,?,?,'pending')
            """,
            (request_id, from_user_id, to_user_id),
        )
        return request_id

    async def list_pending_for(self, to_user_id: str) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM dm_contact_requests "
            "WHERE to_user_id=? AND status='pending' "
            "ORDER BY created_at DESC",
            (to_user_id,),
        )
        return rows_to_dicts(rows)

    async def set_status(self, request_id: str, status: str) -> None:
        if status not in ("pending", "accepted", "declined"):
            raise ValueError(f"invalid status {status!r}")
        await self._db.enqueue(
            "UPDATE dm_contact_requests SET status=? WHERE id=?",
            (status, request_id),
        )

    async def get(self, request_id: str) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM dm_contact_requests WHERE id=?",
            (request_id,),
        )
        return row_to_dict(row)

    async def latest_between(self, from_user_id: str, to_user_id: str) -> dict | None:
        """The newest request from one person to another — a pending one
        first, so an older answer never hides a request still waiting."""
        row = await self._db.fetchone(
            "SELECT * FROM dm_contact_requests "
            "WHERE from_user_id=? AND to_user_id=? "
            "ORDER BY status='pending' DESC, created_at DESC, rowid DESC "
            "LIMIT 1",
            (from_user_id, to_user_id),
        )
        return row_to_dict(row)

    async def answer_pending(
        self, from_user_id: str, to_user_id: str, status: str
    ) -> int:
        """Answer every *pending* request from ``from_user_id`` to
        ``to_user_id`` — the one atomic step two devices race on. Returns
        how many it moved: ``0`` means someone answered first."""
        if status not in ("accepted", "declined"):
            raise ValueError(f"invalid contact request answer {status!r}")
        return await self._db.enqueue_rowcount(
            "UPDATE dm_contact_requests SET status=? "
            "WHERE from_user_id=? AND to_user_id=? AND status='pending'",
            (status, from_user_id, to_user_id),
        )
