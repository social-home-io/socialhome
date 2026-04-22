"""Space-bot repository — persistence for named bot personas.

The :class:`AbstractSpaceBotRepo` Protocol is the service-facing surface;
:class:`SqliteSpaceBotRepo` implements it against the ``space_bots`` table.

Token handling lives inside this repo — the service layer never sees the
raw sha256 hashing or the urlsafe token generation. ``create()`` and
``rotate_token()`` return the plaintext token so the caller can show it
to the user exactly once; the DB only ever stores the hash.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.space_bot import (
    BOT_TOKEN_PREFIX,
    BotScope,
    SpaceBot,
    SpaceBotSlugTakenError,
)
from ..utils.datetime import parse_iso8601_optional
from .base import row_to_dict, rows_to_dicts


def _hash_token(raw: str) -> str:
    """sha256 hex — the only form of a bot token we persist."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _generate_token() -> str:
    """Generate a fresh plaintext bot token (``shb_`` + 40 url-safe chars).

    Matches the Bearer-token format documented for the bot-bridge. The
    prefix makes accidentally leaked tokens easy to find in logs or
    configs. Entropy is 240 bits before base64 — far beyond what any
    online attacker could brute-force.
    """
    return BOT_TOKEN_PREFIX + secrets.token_urlsafe(30)


@runtime_checkable
class AbstractSpaceBotRepo(Protocol):
    async def get(self, bot_id: str) -> SpaceBot | None: ...
    async def get_by_token_hash(self, token_hash: str) -> SpaceBot | None: ...
    async def list_for_space(self, space_id: str) -> list[SpaceBot]: ...
    async def list_for_member(self, space_id: str, user_id: str) -> list[SpaceBot]: ...
    async def create(
        self,
        *,
        bot_id: str,
        space_id: str,
        scope: BotScope,
        slug: str,
        name: str,
        icon: str,
        created_by: str,
    ) -> tuple[SpaceBot, str]:
        """Create a bot and return ``(bot, raw_token)``.

        The raw token is shown to the caller once and never persisted.
        Raises :class:`SpaceBotSlugTakenError` on UNIQUE collision.
        """

    async def update(
        self,
        bot_id: str,
        *,
        name: str | None = None,
        icon: str | None = None,
    ) -> SpaceBot | None:
        """Partial update. ``name=None`` and ``icon=None`` = leave unchanged."""

    async def delete(self, bot_id: str) -> None: ...

    async def rotate_token(self, bot_id: str) -> tuple[SpaceBot, str] | None:
        """Issue a fresh token, invalidating the old hash."""


class SqliteSpaceBotRepo:
    """SQLite-backed :class:`AbstractSpaceBotRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def get(self, bot_id: str) -> SpaceBot | None:
        row = await self._db.fetchone(
            "SELECT * FROM space_bots WHERE bot_id=?",
            (bot_id,),
        )
        return _row_to_bot(row_to_dict(row))

    async def get_by_token_hash(self, token_hash: str) -> SpaceBot | None:
        # Hot path: hit on every POST /api/bot-bridge/spaces/*. Must use
        # the UNIQUE index on token_hash — do not broaden to LIKE / prefix.
        row = await self._db.fetchone(
            "SELECT * FROM space_bots WHERE token_hash=?",
            (token_hash,),
        )
        return _row_to_bot(row_to_dict(row))

    async def list_for_space(self, space_id: str) -> list[SpaceBot]:
        rows = await self._db.fetchall(
            "SELECT * FROM space_bots WHERE space_id=? ORDER BY scope, name COLLATE NOCASE",
            (space_id,),
        )
        return [b for b in (_row_to_bot(d) for d in rows_to_dicts(rows)) if b]

    async def list_for_member(self, space_id: str, user_id: str) -> list[SpaceBot]:
        rows = await self._db.fetchall(
            "SELECT * FROM space_bots "
            "WHERE space_id=? AND scope='member' AND created_by=? "
            "ORDER BY name COLLATE NOCASE",
            (space_id, user_id),
        )
        return [b for b in (_row_to_bot(d) for d in rows_to_dicts(rows)) if b]

    async def create(
        self,
        *,
        bot_id: str,
        space_id: str,
        scope: BotScope,
        slug: str,
        name: str,
        icon: str,
        created_by: str,
    ) -> tuple[SpaceBot, str]:
        raw_token = _generate_token()
        token_hash = _hash_token(raw_token)
        now = datetime.now(timezone.utc)
        try:
            await self._db.enqueue(
                """
                INSERT INTO space_bots(
                    bot_id, space_id, scope, slug, name, icon,
                    created_by, token_hash, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    bot_id,
                    space_id,
                    scope.value,
                    slug,
                    name,
                    icon,
                    created_by,
                    token_hash,
                    now.isoformat(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            # Only the (space_id, scope, slug) uniqueness is user-visible;
            # token_hash collisions are astronomically unlikely but would
            # also land here. Slug collision message is fine for both.
            raise SpaceBotSlugTakenError(
                f"bot slug {slug!r} already exists in this space"
            ) from exc
        bot = SpaceBot(
            bot_id=bot_id,
            space_id=space_id,
            scope=scope,
            slug=slug,
            name=name,
            icon=icon,
            created_by=created_by,
            token_hash=token_hash,
            created_at=now,
        )
        return bot, raw_token

    async def update(
        self,
        bot_id: str,
        *,
        name: str | None = None,
        icon: str | None = None,
    ) -> SpaceBot | None:
        # Build a narrow UPDATE so unspecified fields stay untouched.
        sets: list[str] = []
        params: list[object] = []
        if name is not None:
            sets.append("name=?")
            params.append(name)
        if icon is not None:
            sets.append("icon=?")
            params.append(icon)
        if not sets:
            return await self.get(bot_id)
        params.append(bot_id)
        await self._db.enqueue(
            f"UPDATE space_bots SET {', '.join(sets)} WHERE bot_id=?",
            tuple(params),
        )
        return await self.get(bot_id)

    async def delete(self, bot_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM space_bots WHERE bot_id=?",
            (bot_id,),
        )

    async def rotate_token(self, bot_id: str) -> tuple[SpaceBot, str] | None:
        existing = await self.get(bot_id)
        if existing is None:
            return None
        raw_token = _generate_token()
        token_hash = _hash_token(raw_token)
        await self._db.enqueue(
            "UPDATE space_bots SET token_hash=? WHERE bot_id=?",
            (token_hash, bot_id),
        )
        refreshed = await self.get(bot_id)
        return (refreshed, raw_token) if refreshed else None


def _row_to_bot(row: dict | None) -> SpaceBot | None:
    if row is None:
        return None
    return SpaceBot(
        bot_id=row["bot_id"],
        space_id=row["space_id"],
        scope=BotScope(row["scope"]),
        slug=row["slug"],
        name=row["name"],
        icon=row["icon"],
        created_by=row["created_by"],
        token_hash=row["token_hash"],
        created_at=parse_iso8601_optional(row.get("created_at"))
        or datetime.now(timezone.utc),
    )
