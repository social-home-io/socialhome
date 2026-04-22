"""Conversation / DM repository (§23.47).

Covers:

* :class:`~socialhome.domain.conversation.Conversation` — 1:1 or group DM.
* Members (``conversation_members`` for local users, ``conversation_remote_members``
  for remote peers).
* Messages (``conversation_messages``).
* Per-message reactions (``message_reactions``).

Services are responsible for authorising membership, rendering display
names, and driving delivery. This module is thin data access.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.conversation import (
    Conversation,
    ConversationMember,
    ConversationMessage,
    ConversationType,
    MessageReaction,
    RemoteConversationMember,
)
from .base import bool_col, row_to_dict, rows_to_dicts


@runtime_checkable
class AbstractConversationRepo(Protocol):
    # Conversations -------------------------------------------------------
    async def create(self, conv: Conversation) -> Conversation: ...
    async def get(self, conversation_id: str) -> Conversation | None: ...
    async def list_for_user(self, username: str) -> list[Conversation]: ...
    async def touch_last_message(
        self,
        conversation_id: str,
        *,
        at: str | None = None,
    ) -> None: ...

    # Members -------------------------------------------------------------
    async def add_member(self, member: ConversationMember) -> None: ...
    async def add_remote_member(self, member: RemoteConversationMember) -> None: ...
    async def list_members(self, conversation_id: str) -> list[ConversationMember]: ...
    async def list_remote_members(
        self,
        conversation_id: str,
    ) -> list[RemoteConversationMember]: ...
    async def set_last_read(
        self,
        conversation_id: str,
        username: str,
        *,
        at: str | None = None,
    ) -> None: ...
    async def soft_leave(
        self,
        conversation_id: str,
        username: str,
        *,
        at: str | None = None,
    ) -> None: ...

    # Messages ------------------------------------------------------------
    async def save_message(
        self, message: ConversationMessage
    ) -> ConversationMessage: ...
    async def get_message(self, message_id: str) -> ConversationMessage | None: ...
    async def list_messages(
        self,
        conversation_id: str,
        *,
        before: str | None = None,
        limit: int = 50,
    ) -> list[ConversationMessage]: ...
    async def list_messages_since(
        self,
        conversation_id: str,
        since_iso: str | None,
        *,
        limit: int = 500,
    ) -> list[ConversationMessage]: ...
    async def list_conversations_with_remote_member(
        self,
        instance_id: str,
    ) -> list[str]: ...
    async def soft_delete_message(self, message_id: str) -> None: ...
    async def edit_message(self, message_id: str, new_content: str) -> None: ...
    async def count_unread(self, conversation_id: str, username: str) -> int: ...

    # Reactions -----------------------------------------------------------
    async def add_reaction(
        self,
        message_id: str,
        user_id: str,
        emoji: str,
    ) -> None: ...
    async def remove_reaction(
        self,
        message_id: str,
        user_id: str,
        emoji: str,
    ) -> None: ...
    async def list_reactions(self, message_id: str) -> list[MessageReaction]: ...


class SqliteConversationRepo:
    """SQLite-backed :class:`AbstractConversationRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── Conversations ──────────────────────────────────────────────────

    async def create(self, conv: Conversation) -> Conversation:
        await self._db.enqueue(
            """
            INSERT INTO conversations(
                id, type, name, created_at, last_message_at, bot_enabled
            ) VALUES(?,?,?, COALESCE(?, datetime('now')), ?, ?)
            """,
            (
                conv.id,
                conv.type.value,
                conv.name,
                _iso(conv.created_at),
                _iso(conv.last_message_at),
                int(conv.bot_enabled),
            ),
        )
        return conv

    async def get(self, conversation_id: str) -> Conversation | None:
        row = await self._db.fetchone(
            "SELECT * FROM conversations WHERE id=?",
            (conversation_id,),
        )
        return _row_to_conv(row_to_dict(row))

    async def list_for_user(self, username: str) -> list[Conversation]:
        """Return conversations the local user participates in.

        Excludes conversations the user has soft-left
        (``conversation_members.deleted_at IS NOT NULL``). Ordered by
        ``last_message_at DESC`` so the most active chats come first.
        """
        rows = await self._db.fetchall(
            """
            SELECT c.* FROM conversations c
              JOIN conversation_members m ON m.conversation_id = c.id
             WHERE m.username = ? AND m.deleted_at IS NULL
             ORDER BY COALESCE(c.last_message_at, c.created_at) DESC
            """,
            (username,),
        )
        return [c for c in (_row_to_conv(d) for d in rows_to_dicts(rows)) if c]

    async def touch_last_message(
        self,
        conversation_id: str,
        *,
        at: str | None = None,
    ) -> None:
        await self._db.enqueue(
            "UPDATE conversations SET last_message_at=COALESCE(?, datetime('now')) "
            "WHERE id=?",
            (at, conversation_id),
        )

    # ── Members ────────────────────────────────────────────────────────

    async def add_member(self, member: ConversationMember) -> None:
        await self._db.enqueue(
            """
            INSERT INTO conversation_members(
                conversation_id, username, joined_at, last_read_at,
                history_visible_from, deleted_at
            ) VALUES(?, ?, COALESCE(?, datetime('now')), ?, ?, ?)
            ON CONFLICT(conversation_id, username) DO UPDATE SET
                last_read_at=excluded.last_read_at,
                history_visible_from=excluded.history_visible_from,
                deleted_at=excluded.deleted_at
            """,
            (
                member.conversation_id,
                member.username,
                member.joined_at,
                member.last_read_at,
                member.history_visible_from,
                member.deleted_at,
            ),
        )

    async def add_remote_member(
        self,
        member: RemoteConversationMember,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO conversation_remote_members(
                conversation_id, instance_id, remote_username,
                joined_at, history_visible_from
            ) VALUES(?, ?, ?, COALESCE(?, datetime('now')), ?)
            ON CONFLICT(conversation_id, instance_id, remote_username)
            DO UPDATE SET history_visible_from=excluded.history_visible_from
            """,
            (
                member.conversation_id,
                member.instance_id,
                member.remote_username,
                member.joined_at,
                member.history_visible_from,
            ),
        )

    async def list_members(
        self,
        conversation_id: str,
    ) -> list[ConversationMember]:
        rows = await self._db.fetchall(
            "SELECT * FROM conversation_members WHERE conversation_id=? "
            "ORDER BY joined_at",
            (conversation_id,),
        )
        return [
            ConversationMember(
                conversation_id=r["conversation_id"],
                username=r["username"],
                joined_at=r["joined_at"],
                last_read_at=r["last_read_at"],
                history_visible_from=r["history_visible_from"],
                deleted_at=r["deleted_at"],
            )
            for r in rows
        ]

    async def list_remote_members(
        self,
        conversation_id: str,
    ) -> list[RemoteConversationMember]:
        rows = await self._db.fetchall(
            "SELECT * FROM conversation_remote_members WHERE conversation_id=? "
            "ORDER BY joined_at",
            (conversation_id,),
        )
        return [
            RemoteConversationMember(
                conversation_id=r["conversation_id"],
                instance_id=r["instance_id"],
                remote_username=r["remote_username"],
                joined_at=r["joined_at"],
                history_visible_from=r["history_visible_from"],
            )
            for r in rows
        ]

    async def set_last_read(
        self,
        conversation_id: str,
        username: str,
        *,
        at: str | None = None,
    ) -> None:
        await self._db.enqueue(
            """
            UPDATE conversation_members
               SET last_read_at=COALESCE(?, datetime('now'))
             WHERE conversation_id=? AND username=?
            """,
            (at, conversation_id, username),
        )

    async def soft_leave(
        self,
        conversation_id: str,
        username: str,
        *,
        at: str | None = None,
    ) -> None:
        """Mark a 1:1 DM as hidden from a participant's sidebar.

        For group DMs the spec keeps ``deleted_at`` null — removal for a
        group DM is handled via a separate flow. The service layer decides
        which to call.
        """
        await self._db.enqueue(
            """
            UPDATE conversation_members
               SET deleted_at=COALESCE(?, datetime('now'))
             WHERE conversation_id=? AND username=?
            """,
            (at, conversation_id, username),
        )

    # ── Messages ───────────────────────────────────────────────────────

    async def save_message(
        self,
        message: ConversationMessage,
    ) -> ConversationMessage:
        await self._db.enqueue(
            """
            INSERT INTO conversation_messages(
                id, conversation_id, sender_user_id, content, type, media_url,
                reply_to_id, deleted, edited_at, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?, COALESCE(?, datetime('now')))
            ON CONFLICT(id) DO UPDATE SET
                content=excluded.content,
                media_url=excluded.media_url,
                type=excluded.type,
                reply_to_id=excluded.reply_to_id,
                deleted=excluded.deleted,
                edited_at=excluded.edited_at
            """,
            (
                message.id,
                message.conversation_id,
                message.sender_user_id,
                message.content,
                message.type,
                message.media_url,
                message.reply_to_id,
                int(message.deleted),
                _iso(message.edited_at),
                _iso(message.created_at),
            ),
        )
        # Bump conversation timestamp so list_for_user ordering is fresh.
        await self.touch_last_message(
            message.conversation_id,
            at=_iso(message.created_at),
        )
        return message

    async def get_message(
        self,
        message_id: str,
    ) -> ConversationMessage | None:
        row = await self._db.fetchone(
            "SELECT * FROM conversation_messages WHERE id=?",
            (message_id,),
        )
        return _row_to_message(row_to_dict(row))

    async def list_messages(
        self,
        conversation_id: str,
        *,
        before: str | None = None,
        limit: int = 50,
    ) -> list[ConversationMessage]:
        if before is None:
            rows = await self._db.fetchall(
                """
                SELECT * FROM conversation_messages
                 WHERE conversation_id=?
                 ORDER BY created_at DESC LIMIT ?
                """,
                (conversation_id, int(limit)),
            )
        else:
            rows = await self._db.fetchall(
                """
                SELECT * FROM conversation_messages
                 WHERE conversation_id=? AND created_at < ?
                 ORDER BY created_at DESC LIMIT ?
                """,
                (conversation_id, before, int(limit)),
            )
        return [m for m in (_row_to_message(d) for d in rows_to_dicts(rows)) if m]

    async def list_messages_since(
        self,
        conversation_id: str,
        since_iso: str | None,
        *,
        limit: int = 500,
    ) -> list[ConversationMessage]:
        """Return messages newer than ``since_iso`` (ASC).

        Used by the DM history sync provider to stream the tail of a
        conversation to a peer that missed some messages.  ``since_iso``
        is an inclusive lower bound — a ``None`` value means "from the
        beginning of time" (first-time sync).
        """
        if since_iso:
            rows = await self._db.fetchall(
                """
                SELECT * FROM conversation_messages
                 WHERE conversation_id=? AND created_at > ?
                 ORDER BY created_at ASC LIMIT ?
                """,
                (conversation_id, since_iso, int(limit)),
            )
        else:
            rows = await self._db.fetchall(
                """
                SELECT * FROM conversation_messages
                 WHERE conversation_id=?
                 ORDER BY created_at ASC LIMIT ?
                """,
                (conversation_id, int(limit)),
            )
        return [m for m in (_row_to_message(d) for d in rows_to_dicts(rows)) if m]

    async def list_conversations_with_remote_member(
        self,
        instance_id: str,
    ) -> list[str]:
        """Return conversation ids that have ``instance_id`` as a remote
        participant. Feeds the DM history scheduler on peer reconnect.
        """
        rows = await self._db.fetchall(
            """
            SELECT DISTINCT conversation_id
              FROM conversation_remote_members
             WHERE instance_id=?
            """,
            (instance_id,),
        )
        return [r["conversation_id"] for r in rows]

    async def soft_delete_message(self, message_id: str) -> None:
        await self._db.enqueue(
            "UPDATE conversation_messages "
            "SET deleted=1, content='', media_url=NULL WHERE id=?",
            (message_id,),
        )

    async def edit_message(
        self,
        message_id: str,
        new_content: str,
    ) -> None:
        await self._db.enqueue(
            "UPDATE conversation_messages "
            "SET content=?, edited_at=datetime('now') WHERE id=?",
            (new_content, message_id),
        )

    async def count_unread(
        self,
        conversation_id: str,
        username: str,
    ) -> int:
        """Count messages newer than this member's ``last_read_at``.

        Messages the user sent themselves are excluded via the
        ``sender_user_id != username`` heuristic (the sender's own messages
        never count as "unread"; we resolve username → user_id via the
        users table).
        """
        return int(
            await self._db.fetchval(
                """
            SELECT COUNT(*) FROM conversation_messages m
              LEFT JOIN users u ON u.username = ?
             WHERE m.conversation_id = ?
               AND (u.user_id IS NULL OR m.sender_user_id != u.user_id)
               AND m.deleted = 0
               AND m.created_at > COALESCE(
                     (SELECT last_read_at FROM conversation_members
                       WHERE conversation_id=? AND username=?),
                     '1970-01-01')
            """,
                (username, conversation_id, conversation_id, username),
                default=0,
            )
        )

    # ── Reactions ──────────────────────────────────────────────────────

    async def add_reaction(
        self,
        message_id: str,
        user_id: str,
        emoji: str,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT OR IGNORE INTO message_reactions(
                message_id, user_id, emoji
            ) VALUES(?, ?, ?)
            """,
            (message_id, user_id, emoji),
        )

    async def remove_reaction(
        self,
        message_id: str,
        user_id: str,
        emoji: str,
    ) -> None:
        await self._db.enqueue(
            "DELETE FROM message_reactions "
            "WHERE message_id=? AND user_id=? AND emoji=?",
            (message_id, user_id, emoji),
        )

    async def list_reactions(
        self,
        message_id: str,
    ) -> list[MessageReaction]:
        rows = await self._db.fetchall(
            "SELECT * FROM message_reactions WHERE message_id=? ORDER BY reacted_at",
            (message_id,),
        )
        return [
            MessageReaction(
                message_id=r["message_id"],
                user_id=r["user_id"],
                emoji=r["emoji"],
                reacted_at=_parse(r["reacted_at"]) or datetime.now(timezone.utc),
            )
            for r in rows
        ]


# ─── Helpers ──────────────────────────────────────────────────────────────


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _row_to_conv(row: dict | None) -> Conversation | None:
    if row is None:
        return None
    return Conversation(
        id=row["id"],
        type=ConversationType(row["type"]),
        created_at=_parse(row["created_at"]) or datetime.now(timezone.utc),
        name=row.get("name"),
        last_message_at=_parse(row.get("last_message_at")),
        bot_enabled=bool_col(row.get("bot_enabled", 0)),
    )


def _row_to_message(row: dict | None) -> ConversationMessage | None:
    if row is None:
        return None
    return ConversationMessage(
        id=row["id"],
        conversation_id=row["conversation_id"],
        sender_user_id=row["sender_user_id"],
        content=row.get("content") or "",
        created_at=_parse(row["created_at"]) or datetime.now(timezone.utc),
        type=row.get("type", "text"),
        media_url=row.get("media_url"),
        reply_to_id=row.get("reply_to_id"),
        deleted=bool_col(row.get("deleted", 0)),
        edited_at=_parse(row.get("edited_at")),
    )


def new_conversation(
    *,
    type: ConversationType = ConversationType.DM,
    name: str | None = None,
) -> Conversation:
    return Conversation(
        id=uuid.uuid4().hex,
        type=type,
        name=name,
        created_at=datetime.now(timezone.utc),
    )
