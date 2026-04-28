"""Shared local-credential helpers for adapters that store passwords.

Both :class:`StandaloneAdapter` and :class:`HaAdapter` (when the
operator picked a local-password owner during the wizard) keep
credentials in the ``platform_users`` table and bearer-issued sessions
in ``platform_tokens``. The flows are identical — only the *source* of
the principal differs (standalone has its own users; ha mirrors HA
persons that the operator picked). This module owns those flows.

:class:`LocalCredentialStore` wraps an :class:`AsyncDatabase` and
exposes:

* ``hash_password(password)`` — scrypt envelope writer
* ``verify_password(password, stored)`` — constant-time compare
* ``provision_admin(username, password)`` — first-boot seed
* ``issue_bearer_token(username, password)`` — login → token
* ``authenticate_bearer(token)`` — token → :class:`ExternalUser`

It does NOT manage ``users`` rows — that's the calling adapter's
responsibility, since ``users`` is the domain table and the adapter
controls its identity model. The store only owns ``platform_users``
and ``platform_tokens``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .adapter import ExternalUser

if TYPE_CHECKING:
    from ..db import AsyncDatabase

log = logging.getLogger(__name__)


def _sha256(token: str) -> str:
    """Return the hex SHA-256 digest of the raw token string."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Return a scrypt hash in ``scrypt$<N>$<r>$<p>$<salt_hex>$<hash_hex>`` form.

    Stdlib-only — ``hashlib.scrypt`` is available on every Python 3.14
    build we target. Parameters are chosen to be secure for a household
    server (N=2^14, r=8, p=1 — ~50 ms on commodity hardware).
    """
    n = 2**14
    r = 8
    p = 1
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time compare of ``password`` against a scrypt envelope."""
    if not stored.startswith("scrypt$"):
        return False
    try:
        _, n_s, r_s, p_s, salt_hex, hash_hex = stored.split("$", 5)
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        dk = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
        )
        return hmac.compare_digest(dk, expected)
    except ValueError, KeyError:
        return False


class LocalCredentialStore:
    """Shared password + bearer-token logic for adapters with local
    credentials. Owns ``platform_users`` + ``platform_tokens``.

    Intentionally does not touch the ``users`` table — the calling
    adapter writes that, since the identity model (user_id derivation,
    is_admin policy) belongs to the adapter, not the credential store.
    """

    __slots__ = ("_db",)

    def __init__(self, db: "AsyncDatabase") -> None:
        self._db = db

    async def authenticate_bearer(self, token: str) -> ExternalUser | None:
        """Return the principal for a raw bearer token, or ``None``.

        Joins ``platform_tokens`` to ``platform_users`` and rejects
        expired tokens. Used by both the standalone auth provider and
        the ha auth provider's local-password fallback.
        """
        token_hash = _sha256(token)
        row = await self._db.fetchone(
            """
            SELECT
                u.username,
                u.display_name,
                u.picture_url,
                u.is_admin,
                u.email,
                t.expires_at
            FROM platform_tokens t
            JOIN platform_users u ON u.username = t.username
            WHERE t.token_hash = ?
            """,
            (token_hash,),
        )
        if row is None:
            return None
        if row["expires_at"] is not None:
            try:
                expires = datetime.fromisoformat(row["expires_at"])
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > expires:
                    return None
            except ValueError:
                return None
        return ExternalUser(
            username=row["username"],
            display_name=row["display_name"],
            picture_url=row["picture_url"],
            is_admin=bool(row["is_admin"]),
            email=row["email"],
        )

    async def issue_bearer_token(
        self,
        username: str,
        password: str,
        *,
        label: str = "web",
    ) -> str | None:
        """Verify credentials and mint a fresh bearer token.

        Stores the SHA-256 hash in BOTH ``platform_tokens`` (the
        platform-layer session log) and ``api_tokens`` (the
        application-layer mirror used by the bearer auth strategy).
        Without the api_tokens mirror, ``GET /api/me`` would 401 the
        moment after a successful login.

        Returns the raw token string the client must present, or
        ``None`` if the credentials are invalid.
        """
        row = await self._db.fetchone(
            "SELECT password_hash FROM platform_users WHERE username=?",
            (username,),
        )
        if row is None or not row["password_hash"]:
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        raw = secrets.token_urlsafe(32)
        token_id = secrets.token_urlsafe(16)
        token_hash = _sha256(raw)
        await self._db.enqueue(
            "INSERT INTO platform_tokens(token_id, username, token_hash) VALUES(?,?,?)",
            (token_id, username, token_hash),
        )
        # Mirror into ``api_tokens`` keyed by user_id so the application
        # bearer strategy resolves the principal. Skip silently when the
        # adapter hasn't seeded a matching ``users`` row yet (legacy
        # deployments) — the platform_tokens row is still useful for
        # audit and the test fixture exercises this branch.
        user_row = await self._db.fetchone(
            "SELECT user_id FROM users WHERE username=?",
            (username,),
        )
        if user_row is not None and user_row["user_id"]:
            await self._db.enqueue(
                """
                INSERT INTO api_tokens(token_id, user_id, label, token_hash)
                VALUES(?, ?, ?, ?)
                """,
                (token_id, user_row["user_id"], label, token_hash),
            )
        return raw

    async def set_password(
        self,
        username: str,
        password: str,
        *,
        display_name: str | None = None,
        is_admin: bool = False,
    ) -> None:
        """Insert / update the ``platform_users`` row for ``username``.

        Used by the ha-mode setup wizard to attach a password to a
        picked HA person. Idempotent — re-running with a new password
        rotates the hash.
        """
        pw_hash = hash_password(password)
        await self._db.enqueue(
            """
            INSERT INTO platform_users(
                username, display_name, is_admin, password_hash
            ) VALUES(?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                password_hash=excluded.password_hash,
                display_name=excluded.display_name,
                is_admin=excluded.is_admin
            """,
            (username, display_name or username, 1 if is_admin else 0, pw_hash),
        )
