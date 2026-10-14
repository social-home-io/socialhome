"""Auth audit log — append-only trail for password-bearing events.

Every login attempt, every admin-issued password reset, and every
redeem of a reset token (success or failure) writes a row. The
admin can read this back via ``GET /api/admin/auth-audit`` to spot
brute-force attempts or to correlate "I can't sign in" reports with
what the server saw.

The repo is intentionally minimal: ``record`` (insert) and
``list_recent`` (read for admin UI). Schema lives in
``0001_initial.sql`` under "Auth audit log".

Event types currently in use:

* ``login_success`` — a valid bearer was issued via /api/auth/token.
* ``login_failure`` — credentials didn't match.
* ``reset_issue`` — admin minted a reset token for a user.
* ``reset_redeem_success`` — a reset token was consumed and the
  password rotated.
* ``reset_redeem_failure`` — token unknown / expired / already used.
* ``display_ha_unlock`` — Home Assistant lifted a wall panel's privacy
  lock without the PIN (``metadata``: ``display_id``, ``display_name``;
  never a token or hash).
"""

from __future__ import annotations

import uuid
from typing import Protocol

import orjson

from ..db import AsyncDatabase


class AbstractAuthAuditLogRepo(Protocol):
    async def record(
        self,
        event_type: str,
        *,
        username: str | None = None,
        ip_address: str | None = None,
        metadata: dict | None = None,
    ) -> None: ...

    async def list_recent(self, limit: int = 100) -> list[dict]: ...

    async def prune_older_than(self, *, days: int = 90) -> int: ...


class SqliteAuthAuditLogRepo:
    """SQLite-backed audit log."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def record(
        self,
        event_type: str,
        *,
        username: str | None = None,
        ip_address: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO auth_audit_log(
                id, event_type, username, ip_address, metadata
            ) VALUES(?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                event_type,
                username,
                ip_address,
                orjson.dumps(metadata).decode() if metadata else None,
            ),
        )

    async def list_recent(self, limit: int = 100) -> list[dict]:
        rows = await self._db.fetchall(
            """
            SELECT id, event_type, username, ip_address, metadata, created_at
              FROM auth_audit_log
             ORDER BY created_at DESC, rowid DESC
             LIMIT ?
            """,
            (limit,),
        )
        out: list[dict] = []
        for r in rows:
            row = dict(r)
            if row.get("metadata"):
                try:
                    row["metadata"] = orjson.loads(row["metadata"])
                except ValueError, TypeError:
                    row["metadata"] = None
            out.append(row)
        return out

    async def prune_older_than(self, *, days: int = 90) -> int:
        """Delete audit rows older than ``days``. Returns the count deleted.

        Bounds the table — it's an append-only trail (a row per login attempt,
        incl. failures), so without this an attacker can grow it via repeated
        failed logins. 90 days is plenty of history for spotting brute force.
        """
        before = await self._db.fetchval(
            "SELECT COUNT(*) FROM auth_audit_log WHERE created_at < datetime('now', ?)",
            (f"-{int(days)} days",),
            default=0,
        )
        await self._db.enqueue(
            "DELETE FROM auth_audit_log WHERE created_at < datetime('now', ?)",
            (f"-{int(days)} days",),
        )
        return int(before)
