"""Profile-picture blob storage (§23 profile).

Owns two tables:

* ``user_profile_pictures`` — one row per user, holds the current
  household-level WebP bytes.
* ``space_member_profile_pictures`` — one row per ``(space_id,
  user_id)``, the member's space-scoped override.

Both rows carry a short hex digest (``hash``) used for cache-busting
URLs and as the payload key in federation events. The bytes never
enter the domain layer — :class:`User` / :class:`SpaceMember`
dataclasses only carry the hash.
"""

from __future__ import annotations

import hashlib
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase


def compute_picture_hash(bytes_webp: bytes) -> str:
    """Return the 16-char SHA-256 hex prefix for cache-busting URLs."""
    return hashlib.sha256(bytes_webp).hexdigest()[:16]


@runtime_checkable
class AbstractProfilePictureRepo(Protocol):
    # Household -----------------------------------------------------------
    async def get_user_picture(
        self,
        user_id: str,
    ) -> tuple[bytes, str] | None: ...
    async def set_user_picture(
        self,
        user_id: str,
        *,
        bytes_webp: bytes,
        hash: str,
        width: int,
        height: int,
    ) -> None: ...
    async def clear_user_picture(self, user_id: str) -> None: ...

    # Space -------------------------------------------------------------
    async def get_member_picture(
        self,
        space_id: str,
        user_id: str,
    ) -> tuple[bytes, str] | None: ...
    async def set_member_picture(
        self,
        space_id: str,
        user_id: str,
        *,
        bytes_webp: bytes,
        hash: str,
        width: int,
        height: int,
    ) -> None: ...
    async def clear_member_picture(
        self,
        space_id: str,
        user_id: str,
    ) -> None: ...


class SqliteProfilePictureRepo:
    """SQLite implementation of :class:`AbstractProfilePictureRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── Household pictures ─────────────────────────────────────────────

    async def get_user_picture(
        self,
        user_id: str,
    ) -> tuple[bytes, str] | None:
        row = await self._db.fetchone(
            "SELECT bytes_webp, hash FROM user_profile_pictures WHERE user_id=?",
            (user_id,),
        )
        if row is None:
            return None
        return bytes(row["bytes_webp"]), str(row["hash"])

    async def set_user_picture(
        self,
        user_id: str,
        *,
        bytes_webp: bytes,
        hash: str,
        width: int,
        height: int,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO user_profile_pictures(
                user_id, bytes_webp, hash, width, height, updated_at
            ) VALUES(?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
                bytes_webp=excluded.bytes_webp,
                hash=excluded.hash,
                width=excluded.width,
                height=excluded.height,
                updated_at=excluded.updated_at
            """,
            (user_id, bytes_webp, hash, int(width), int(height)),
        )

    async def clear_user_picture(self, user_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM user_profile_pictures WHERE user_id=?",
            (user_id,),
        )

    # ── Space-scoped pictures ──────────────────────────────────────────

    async def get_member_picture(
        self,
        space_id: str,
        user_id: str,
    ) -> tuple[bytes, str] | None:
        row = await self._db.fetchone(
            "SELECT bytes_webp, hash FROM space_member_profile_pictures "
            "WHERE space_id=? AND user_id=?",
            (space_id, user_id),
        )
        if row is None:
            return None
        return bytes(row["bytes_webp"]), str(row["hash"])

    async def set_member_picture(
        self,
        space_id: str,
        user_id: str,
        *,
        bytes_webp: bytes,
        hash: str,
        width: int,
        height: int,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO space_member_profile_pictures(
                space_id, user_id, bytes_webp, hash, width, height, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(space_id, user_id) DO UPDATE SET
                bytes_webp=excluded.bytes_webp,
                hash=excluded.hash,
                width=excluded.width,
                height=excluded.height,
                updated_at=excluded.updated_at
            """,
            (space_id, user_id, bytes_webp, hash, int(width), int(height)),
        )

    async def clear_member_picture(
        self,
        space_id: str,
        user_id: str,
    ) -> None:
        await self._db.enqueue(
            "DELETE FROM space_member_profile_pictures WHERE space_id=? AND user_id=?",
            (space_id, user_id),
        )
