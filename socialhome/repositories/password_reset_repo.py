"""Password-reset tokens — admin-issued, single-use, 1h TTL.

Standalone mode has no SMTP, so a forgotten password is recovered by
a household admin issuing a one-time token via the admin UI; the
admin then hands the resulting reset URL to the user out-of-band
(in-person, secure messenger, etc.). The user redeems it via
``POST /api/auth/redeem-password-reset``.

Storage stores the SHA-256 of the raw token so a database leak does
not expose redeemable tokens. The repo's ``create_token`` returns the
*raw* token to the caller exactly once; subsequent reads only see the
hash. Mirrors the shape of :class:`SpaceRepo` invite tokens (see
``space_repo.create_invite_token`` / ``consume_invite_token``).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Protocol

from ..db import AsyncDatabase

#: Single-use, single-attempt: tokens are 32 bytes urlsafe-base64
#: (~43 chars). Plenty of entropy against brute force given the
#: rate-limit on the redeem endpoint.
_TOKEN_BYTES = 32

#: Default lifetime. One hour matches the spec note in the migration
#: comment and is short enough that a leaked URL dies fast.
DEFAULT_TTL_SECONDS = 3600


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


class AbstractPasswordResetRepo(Protocol):
    async def create_token(
        self,
        username: str,
        issued_by_admin: str,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        now: datetime | None = None,
    ) -> tuple[str, str]: ...

    async def consume_token(
        self,
        raw_token: str,
        *,
        now: datetime | None = None,
    ) -> str | None: ...

    async def cleanup_expired(self, *, now: datetime | None = None) -> int: ...


class SqlitePasswordResetRepo:
    """SQLite implementation backed by ``password_reset_tokens``."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def create_token(
        self,
        username: str,
        issued_by_admin: str,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        now: datetime | None = None,
    ) -> tuple[str, str]:
        """Mint a token for ``username``.

        Returns ``(raw_token, expires_at_iso)``. The raw token is
        returned exactly once — only its hash is stored. The admin
        copies it onto the reset URL handed to the user.
        """
        raw = secrets.token_urlsafe(_TOKEN_BYTES)
        token_hash = _hash_token(raw)
        when = now or datetime.now(UTC)
        expires_at = (when + timedelta(seconds=ttl_seconds)).isoformat()
        await self._db.enqueue(
            """
            INSERT INTO password_reset_tokens(
                token, username, issued_by_admin, issued_at, expires_at
            ) VALUES(?, ?, ?, ?, ?)
            """,
            (token_hash, username, issued_by_admin, when.isoformat(), expires_at),
        )
        return raw, expires_at

    async def consume_token(
        self,
        raw_token: str,
        *,
        now: datetime | None = None,
    ) -> str | None:
        """Atomically mark a token used and return its ``username``.

        Returns ``None`` if the token does not exist, has expired, or
        has already been consumed. Caller is responsible for the
        password update; this repo only handles token state.
        """
        token_hash = _hash_token(raw_token)
        when = (now or datetime.now(UTC)).isoformat()

        def _run(conn):
            cur = conn.execute(
                """
                UPDATE password_reset_tokens
                   SET used_at = ?
                 WHERE token = ?
                   AND used_at IS NULL
                   AND expires_at > ?
                """,
                (when, token_hash, when),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                """
                SELECT username
                  FROM password_reset_tokens WHERE token=?
                """,
                (token_hash,),
            ).fetchone()
            return row[0] if row else None

        return await self._db.transact(_run)

    async def cleanup_expired(self, *, now: datetime | None = None) -> int:
        """Delete tokens whose TTL has elapsed.

        Idempotent. Used tokens stay around for a short audit trail
        until their TTL also passes — keeps the table tiny.
        """
        when = (now or datetime.now(UTC)).isoformat()

        def _run(conn):
            cur = conn.execute(
                "DELETE FROM password_reset_tokens WHERE expires_at <= ?",
                (when,),
            )
            return cur.rowcount

        return await self._db.transact(_run)
