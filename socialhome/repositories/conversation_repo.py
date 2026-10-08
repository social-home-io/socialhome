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
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.conversation import (
    Conversation,
    ConversationMember,
    ConversationMessage,
    ConversationType,
    GroupRosterChange,
    MessageReaction,
    RemoteConversationMember,
    SystemChatScope,
)
from .base import bool_col, row_to_dict, rows_to_dicts
from .cp_repo import guardian_block_counterparts_sql


@runtime_checkable
class AbstractConversationRepo(Protocol):
    # Conversations -------------------------------------------------------
    async def create(self, conv: Conversation) -> Conversation: ...
    async def get(self, conversation_id: str) -> Conversation | None: ...
    async def list_for_user(self, username: str) -> list[Conversation]: ...
    async def get_household_chat(self) -> Conversation | None: ...
    async def create_system_chat(
        self,
        scope: SystemChatScope,
        *,
        space_id: str | None = None,
        name: str | None = None,
    ) -> Conversation: ...
    async def touch_last_message(
        self,
        conversation_id: str,
        *,
        at: str | None = None,
    ) -> None: ...

    # Members -------------------------------------------------------------
    async def add_member(self, member: ConversationMember) -> None: ...
    async def add_remote_member(self, member: RemoteConversationMember) -> None: ...
    async def upsert_seat(
        self,
        conversation_id: str,
        username: str,
        *,
        notif_level: str,
        at: str | None = None,
    ) -> None: ...
    async def remove_seat(
        self,
        conversation_id: str,
        username: str,
        *,
        at: str | None = None,
    ) -> None: ...
    async def list_members(self, conversation_id: str) -> list[ConversationMember]: ...
    async def list_remote_members(
        self,
        conversation_id: str,
    ) -> list[RemoteConversationMember]: ...
    async def apply_group_roster(
        self,
        conversation: Conversation,
        *,
        local_usernames: Sequence[str],
        remote_members: Sequence[RemoteConversationMember],
        at: str,
    ) -> GroupRosterChange | None: ...
    async def set_last_read(
        self,
        conversation_id: str,
        username: str,
        *,
        at: str | None = None,
    ) -> None: ...
    async def set_muted_until(
        self,
        conversation_id: str,
        username: str,
        muted_until: str | None,
    ) -> None: ...
    async def set_notif_level(
        self,
        conversation_id: str,
        username: str,
        level: str,
    ) -> None: ...
    async def soft_leave(
        self,
        conversation_id: str,
        username: str,
        *,
        at: str | None = None,
        left_version: int | None = None,
    ) -> None: ...
    async def list_fully_left_conversation_ids(self) -> list[str]: ...
    async def hard_delete(self, conversation_id: str) -> None: ...

    # Messages ------------------------------------------------------------
    async def save_message(
        self, message: ConversationMessage
    ) -> ConversationMessage: ...
    async def save_message_returning_created(
        self, message: ConversationMessage
    ) -> tuple[ConversationMessage, bool]: ...
    async def insert_message_if_absent(self, message: ConversationMessage) -> bool: ...
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
    async def list_conversations_with_sender(
        self,
        sender_user_id: str,
    ) -> list[str]: ...
    async def soft_delete_message(self, message_id: str) -> None: ...
    async def soft_delete_messages_by_sender(
        self,
        conversation_id: str,
        sender_user_id: str,
    ) -> int: ...
    async def edit_message(self, message_id: str, new_content: str) -> None: ...
    async def find_pending_audio_transcripts(
        self,
        *,
        since_iso: str,
        limit: int = 50,
    ) -> list[ConversationMessage]: ...
    async def update_media_sync_status(
        self,
        *,
        message_id: str,
        status: str | None,
        media_url: str | None = None,
    ) -> None: ...
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

    # Delivery state (§12.5 — read receipts + delivery tracking) ---------
    async def upsert_delivery_state(
        self,
        *,
        conversation_id: str,
        message_id: str,
        user_id: str,
        state: str,
    ) -> None: ...
    async def list_delivery_states(
        self,
        conversation_id: str,
        *,
        message_ids: list[str] | None = None,
    ) -> list[dict]: ...
    async def mark_conversation_read(
        self,
        *,
        conversation_id: str,
        user_id: str,
        up_to_at: str,
    ) -> int: ...


class SqliteConversationRepo:
    """SQLite-backed :class:`AbstractConversationRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── Conversations ──────────────────────────────────────────────────

    async def create(self, conv: Conversation) -> Conversation:
        await self._db.enqueue(
            """
            INSERT INTO conversations(
                id, type, name, created_at, last_message_at, bot_enabled,
                membership_version, system_scope, space_id
            ) VALUES(?,?,?, COALESCE(?, datetime('now')), ?, ?, ?, ?, ?)
            """,
            (
                conv.id,
                conv.type.value,
                conv.name,
                _iso(conv.created_at),
                _iso(conv.last_message_at),
                int(conv.bot_enabled),
                int(conv.membership_version),
                conv.system_scope.value if conv.system_scope is not None else None,
                conv.space_id,
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

        DM threads (``type='dm'``) where the other participant is on the
        viewer's personal block list are filtered out (§Privacy).
        Group threads (``type='group_dm'``) are not filtered here — a
        per-message hide would be the right call there, and group-level
        gating is out of scope for the v1 block. The blocker can leave
        the group manually if the presence is unwanted.
        """
        rows = await self._db.fetchall(
            """
            SELECT c.* FROM conversations c
              JOIN conversation_members m ON m.conversation_id = c.id
             WHERE m.username = ? AND m.deleted_at IS NULL
               AND c.system_scope IS NULL
               AND NOT (
                   c.type = 'dm'
                   AND EXISTS (
                       SELECT 1
                         FROM conversation_members peer
                         JOIN users peer_user
                           ON peer_user.username = peer.username
                         JOIN users self_user
                           ON self_user.username = m.username
                         JOIN user_blocks ub
                           ON ub.blocker_user_id = self_user.user_id
                          AND ub.blocked_user_id = peer_user.user_id
                        WHERE peer.conversation_id = c.id
                          AND peer.username <> m.username
                          AND peer.deleted_at IS NULL
                   )
               )
             ORDER BY COALESCE(c.last_message_at, c.created_at) DESC
            """,
            (username,),
        )
        return [c for c in (_row_to_conv(d) for d in rows_to_dicts(rows)) if c]

    async def get_household_chat(self) -> Conversation | None:
        """The household chat, or ``None`` before it was first created."""
        row = await self._db.fetchone(
            "SELECT * FROM conversations WHERE system_scope=?",
            (SystemChatScope.HOUSEHOLD.value,),
        )
        return _row_to_conv(row_to_dict(row))

    async def create_system_chat(
        self,
        scope: SystemChatScope,
        *,
        space_id: str | None = None,
        name: str | None = None,
    ) -> Conversation:
        """Create the system chat for ``scope`` (and ``space_id``) unless it
        exists; return the one stored.

        Idempotent and race-safe: the partial unique indexes from migration
        0082 (one household chat, one chat per space) turn a concurrent
        second insert into a no-op, and the row read back is the winner.
        """
        if (scope is SystemChatScope.SPACE) != (space_id is not None):
            raise ValueError("a space chat needs a space_id, and only it")
        conv_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()

        def _run(conn) -> dict | None:
            conn.execute(
                "INSERT OR IGNORE INTO conversations(id, type, name, created_at,"
                " system_scope, space_id) VALUES(?,?,?,?,?,?)",
                (
                    conv_id,
                    ConversationType.GROUP_DM.value,
                    name,
                    now,
                    scope.value,
                    space_id,
                ),
            )
            if space_id is not None:
                cur = conn.execute(
                    "SELECT * FROM conversations WHERE space_id=?", (space_id,)
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM conversations WHERE system_scope=?",
                    (scope.value,),
                )
            row = cur.fetchone()
            if row is None:  # pragma: no cover - the insert or the winner is there
                return None
            return dict(zip([c[0] for c in cur.description], row))

        conv = _row_to_conv(await self._db.transact(_run))
        if conv is None:  # pragma: no cover - see above
            raise RuntimeError("system chat was not stored")
        return conv

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
                joined_at, history_visible_from, user_id, display_name
            ) VALUES(?, ?, ?, COALESCE(?, datetime('now')), ?, ?, ?)
            ON CONFLICT(conversation_id, instance_id, remote_username)
            DO UPDATE SET
                history_visible_from=excluded.history_visible_from,
                user_id=COALESCE(excluded.user_id, user_id),
                display_name=COALESCE(excluded.display_name, display_name)
            """,
            (
                member.conversation_id,
                member.instance_id,
                member.remote_username,
                member.joined_at,
                member.history_visible_from,
                member.user_id,
                member.display_name,
            ),
        )

    async def upsert_seat(
        self,
        conversation_id: str,
        username: str,
        *,
        notif_level: str,
        at: str | None = None,
    ) -> None:
        """Seat ``username`` in a system chat, or bring a removed seat back.

        A new seat starts at ``notif_level`` with its read watermark at
        ``at`` (default now), so a newcomer doesn't inherit the whole
        backlog as unread. An active seat is left exactly as it is; a
        removed one (``deleted_at``) is reactivated with the watermark at
        ``at`` and keeps the member's own level and mute.
        """
        stamp = at or datetime.now(timezone.utc).isoformat()
        await self._db.enqueue(
            """
            INSERT INTO conversation_members(
                conversation_id, username, joined_at, last_read_at, notif_level
            ) VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id, username) DO UPDATE SET
                last_read_at=excluded.last_read_at,
                joined_at=excluded.joined_at,
                deleted_at=NULL,
                left_version=NULL
             WHERE conversation_members.deleted_at IS NOT NULL
            """,
            (conversation_id, username, stamp, stamp, notif_level),
        )

    async def remove_seat(
        self,
        conversation_id: str,
        username: str,
        *,
        at: str | None = None,
    ) -> None:
        """Take ``username``'s seat out of a system chat (soft: the row and
        the member's messages stay; :meth:`upsert_seat` brings it back)."""
        await self._db.enqueue(
            """
            UPDATE conversation_members
               SET deleted_at=COALESCE(?, datetime('now'))
             WHERE conversation_id=? AND username=? AND deleted_at IS NULL
            """,
            (at, conversation_id, username),
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
                joined_version=r["joined_version"],
                left_version=r["left_version"],
                muted_until=r["muted_until"],
                notif_level=r["notif_level"],
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
                user_id=r["user_id"],
                display_name=r["display_name"],
                joined_version=r["joined_version"],
            )
            for r in rows
        ]

    async def apply_group_roster(
        self,
        conversation: Conversation,
        *,
        local_usernames: Sequence[str],
        remote_members: Sequence[RemoteConversationMember],
        at: str,
    ) -> GroupRosterChange | None:
        """Make a group's seats exactly one member-list snapshot, atomically.

        ``conversation.membership_version`` is the snapshot's version. It is
        applied only when newer than the version stored here (a missing row
        counts as version 0), inside one ``BEGIN IMMEDIATE`` transaction, so
        two snapshots racing each other can never interleave and an older
        one never rolls a newer one back. Returns ``None`` for a stale
        snapshot.

        Local members not in ``local_usernames`` are soft-left
        (``deleted_at``); listed ones are seated, or brought back, with
        their read watermark kept. Remote seats not in ``remote_members``
        are deleted; listed ones are upserted with the roster's ``user_id``
        / ``display_name``. The name is taken from the snapshot. Every seat
        this snapshot fills (new, or back after leaving) is stamped with its
        version as ``joined_version``; a returning member's ``left_version``
        marker is cleared.
        """
        conv = conversation
        wanted_local = list(dict.fromkeys(local_usernames))
        wanted_remote = {(m.instance_id, m.remote_username): m for m in remote_members}

        def _run(conn) -> GroupRosterChange | None:
            row = conn.execute(
                "SELECT membership_version FROM conversations WHERE id=?",
                (conv.id,),
            ).fetchone()
            created = row is None
            if not created and int(row[0] or 0) >= conv.membership_version:
                return None
            if created:
                conn.execute(
                    "INSERT INTO conversations(id, type, name, created_at,"
                    " membership_version) VALUES(?,?,?,?,?)",
                    (
                        conv.id,
                        conv.type.value,
                        conv.name,
                        _iso(conv.created_at) or at,
                        conv.membership_version,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE conversations SET name=?, membership_version=? WHERE id=?",
                    (conv.name, conv.membership_version, conv.id),
                )
            current = {
                r[0]: r[1]
                for r in conn.execute(
                    "SELECT username, deleted_at FROM conversation_members"
                    " WHERE conversation_id=?",
                    (conv.id,),
                ).fetchall()
            }
            added: list[str] = []
            for username in wanted_local:
                if username not in current:
                    conn.execute(
                        "INSERT INTO conversation_members(conversation_id,"
                        " username, joined_at, joined_version) VALUES(?,?,?,?)",
                        (conv.id, username, at, conv.membership_version),
                    )
                    added.append(username)
                elif current[username] is not None:
                    conn.execute(
                        "UPDATE conversation_members SET deleted_at=NULL,"
                        " left_version=NULL, joined_at=?, joined_version=?"
                        " WHERE conversation_id=? AND username=?",
                        (at, conv.membership_version, conv.id, username),
                    )
                    added.append(username)
            removed: list[str] = []
            for username, deleted_at in current.items():
                if username in wanted_local or deleted_at is not None:
                    continue
                conn.execute(
                    "UPDATE conversation_members SET deleted_at=?"
                    " WHERE conversation_id=? AND username=?",
                    (at, conv.id, username),
                )
                removed.append(username)
            seats = [
                (r[0], r[1])
                for r in conn.execute(
                    "SELECT instance_id, remote_username"
                    " FROM conversation_remote_members WHERE conversation_id=?",
                    (conv.id,),
                ).fetchall()
            ]
            removed_remote: list[tuple[str, str]] = []
            for key in seats:
                if key in wanted_remote:
                    continue
                conn.execute(
                    "DELETE FROM conversation_remote_members WHERE"
                    " conversation_id=? AND instance_id=? AND remote_username=?",
                    (conv.id, key[0], key[1]),
                )
                removed_remote.append(key)
            for (instance_id, username), m in wanted_remote.items():
                conn.execute(
                    "INSERT INTO conversation_remote_members(conversation_id,"
                    " instance_id, remote_username, joined_at, user_id,"
                    " display_name, joined_version) VALUES(?,?,?,?,?,?,?)"
                    " ON CONFLICT(conversation_id, instance_id, remote_username)"
                    " DO UPDATE SET user_id=excluded.user_id,"
                    " display_name=excluded.display_name",
                    (
                        conv.id,
                        instance_id,
                        username,
                        m.joined_at or at,
                        m.user_id,
                        m.display_name,
                        conv.membership_version,
                    ),
                )
            return GroupRosterChange(
                conversation_id=conv.id,
                version=conv.membership_version,
                created=created,
                added_local=tuple(added),
                removed_local=tuple(removed),
                removed_remote=tuple(removed_remote),
            )

        return await self._db.transact(_run)

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

    async def set_muted_until(
        self,
        conversation_id: str,
        username: str,
        muted_until: str | None,
    ) -> None:
        """Stamp (or with ``None`` clear) the member's own mute."""
        await self._db.enqueue(
            """
            UPDATE conversation_members
               SET muted_until=?
             WHERE conversation_id=? AND username=?
            """,
            (muted_until, conversation_id, username),
        )

    async def set_notif_level(
        self,
        conversation_id: str,
        username: str,
        level: str,
    ) -> None:
        """Set the member's own notification level (``all`` / ``mentions``;
        the column CHECK refuses anything else)."""
        await self._db.enqueue(
            """
            UPDATE conversation_members
               SET notif_level=?
             WHERE conversation_id=? AND username=?
            """,
            (level, conversation_id, username),
        )

    async def soft_leave(
        self,
        conversation_id: str,
        username: str,
        *,
        at: str | None = None,
        left_version: int | None = None,
    ) -> None:
        """Mark a 1:1 DM as hidden from a participant's sidebar.

        ``left_version`` — a group kept by another household: the roster
        version held when the user left (see ``apply_group_roster``).

        For group DMs the spec keeps ``deleted_at`` null — removal for a
        group DM is handled via a separate flow. The service layer decides
        which to call.
        """
        await self._db.enqueue(
            """
            UPDATE conversation_members
               SET deleted_at=COALESCE(?, datetime('now')),
                   left_version=?
             WHERE conversation_id=? AND username=?
            """,
            (at, left_version, conversation_id, username),
        )

    async def list_fully_left_conversation_ids(self) -> list[str]:
        """Conversations whose every local member has ``deleted_at`` set
        and that have no remote members (§23.47c). Federated conversations
        are skipped — their lifecycle is owned by the federation peer. So
        are system chats: a household chat nobody is seated in right now
        (every user deactivated) is kept, not swept.
        """
        rows = await self._db.fetchall(
            """
            SELECT c.id FROM conversations c
            WHERE c.system_scope IS NULL
            AND NOT EXISTS (
                SELECT 1 FROM conversation_members m
                WHERE m.conversation_id = c.id AND m.deleted_at IS NULL
            )
            AND NOT EXISTS (
                SELECT 1 FROM conversation_remote_members rm
                WHERE rm.conversation_id = c.id
            )
            """,
        )
        return [r["id"] for r in rows]

    async def hard_delete(self, conversation_id: str) -> None:
        """Drop the conversation row. ``ON DELETE CASCADE`` removes
        members, messages, reactions, delivery state, and gap rows.
        """
        await self._db.enqueue(
            "DELETE FROM conversations WHERE id=?",
            (conversation_id,),
        )

    # ── Messages ───────────────────────────────────────────────────────

    _MESSAGE_UPSERT_SQL = """
        INSERT INTO conversation_messages(
            id, conversation_id, sender_user_id, content, type, media_url,
            file_name, mime_type, file_size_bytes,
            media_blob_id, media_sync_status,
            reply_to_id, reply_to_highlight_frame_id,
            reply_to_highlight_frame_snapshot,
            deleted, edited_at, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, COALESCE(?, datetime('now')))
        ON CONFLICT(id) DO UPDATE SET
            content=excluded.content,
            media_url=excluded.media_url,
            file_name=excluded.file_name,
            mime_type=excluded.mime_type,
            file_size_bytes=excluded.file_size_bytes,
            media_blob_id=excluded.media_blob_id,
            media_sync_status=excluded.media_sync_status,
            type=excluded.type,
            reply_to_id=excluded.reply_to_id,
            reply_to_highlight_frame_id=excluded.reply_to_highlight_frame_id,
            reply_to_highlight_frame_snapshot=excluded.reply_to_highlight_frame_snapshot,
            deleted=excluded.deleted,
            edited_at=excluded.edited_at
        """

    _MESSAGE_INSERT_IGNORE_SQL = """
        INSERT OR IGNORE INTO conversation_messages(
            id, conversation_id, sender_user_id, content, type,
            media_url, file_name, mime_type, file_size_bytes,
            media_blob_id, media_sync_status,
            reply_to_id, reply_to_highlight_frame_id,
            reply_to_highlight_frame_snapshot,
            deleted, edited_at, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, COALESCE(?, datetime('now')))
        """

    @staticmethod
    def _message_upsert_params(message: ConversationMessage) -> tuple:
        return (
            message.id,
            message.conversation_id,
            message.sender_user_id,
            message.content,
            message.type,
            message.media_url,
            message.file_name,
            message.mime_type,
            message.file_size_bytes,
            message.media_blob_id,
            message.media_sync_status,
            message.reply_to_id,
            message.reply_to_highlight_frame_id,
            message.reply_to_highlight_frame_snapshot,
            int(message.deleted),
            _iso(message.edited_at),
            _iso(message.created_at),
        )

    async def save_message(
        self,
        message: ConversationMessage,
    ) -> ConversationMessage:
        await self._db.enqueue(
            self._MESSAGE_UPSERT_SQL,
            self._message_upsert_params(message),
        )
        # Bump conversation timestamp so list_for_user ordering is fresh.
        await self.touch_last_message(
            message.conversation_id,
            at=_iso(message.created_at),
        )
        return message

    async def save_message_returning_created(
        self,
        message: ConversationMessage,
    ) -> tuple[ConversationMessage, bool]:
        """Upsert *message* and report whether the row was newly
        inserted (True) or replaced an existing row (False).

        Used by the federation inbound DM handler to distinguish
        "this is a brand-new message → publish DmMessageCreated"
        from "this is a redelivery / edit of a message I already
        have → publish DmMessageUpdated (or nothing for an exact
        replay)".

        The check is **race-safe**: a transport that races a second
        copy of the same envelope under perfect-negotiation WebRTC +
        HTTPS-inbox failover sees exactly one ``created=True`` even
        when both copies hit the repo concurrently. The previous
        peek-then-save pattern in the inbound handler raced because
        both transports could see ``existing=None`` before either
        wrote, and both would then publish ``DmMessageCreated`` →
        the user got two bell rows + two pushes for one message.
        ``BEGIN IMMEDIATE`` here serialises against any concurrent
        writer.
        """
        params = self._message_upsert_params(message)

        def _do(conn):
            # ``INSERT OR IGNORE`` returns rowcount=0 when the row
            # already exists; rowcount=1 means we inserted. Either
            # way the conversation timestamp gets bumped below.
            cur = conn.execute(self._MESSAGE_INSERT_IGNORE_SQL, params)
            inserted = cur.rowcount == 1
            if not inserted:
                # Existing row — apply the same UPDATE the upsert
                # path would have done, so an edit / voice-note
                # transcript still patches the bubble in place.
                conn.execute(
                    """
                    UPDATE conversation_messages SET
                        content=?,
                        media_url=?,
                        file_name=?,
                        mime_type=?,
                        file_size_bytes=?,
                        media_blob_id=?,
                        media_sync_status=?,
                        type=?,
                        reply_to_id=?,
                        reply_to_highlight_frame_id=?,
                        reply_to_highlight_frame_snapshot=?,
                        deleted=?,
                        edited_at=?
                    WHERE id=?
                    """,
                    (
                        message.content,
                        message.media_url,
                        message.file_name,
                        message.mime_type,
                        message.file_size_bytes,
                        message.media_blob_id,
                        message.media_sync_status,
                        message.type,
                        message.reply_to_id,
                        message.reply_to_highlight_frame_id,
                        message.reply_to_highlight_frame_snapshot,
                        int(message.deleted),
                        _iso(message.edited_at),
                        message.id,
                    ),
                )
            return inserted

        inserted = await self._db.transact(_do)
        await self.touch_last_message(
            message.conversation_id,
            at=_iso(message.created_at),
        )
        return message, inserted

    async def insert_message_if_absent(self, message: ConversationMessage) -> bool:
        """Insert ``message`` unless a row with its id exists; never update one.

        Returns whether the row was inserted. For catch-up paths (DM history)
        that may fill in a missing message but must never rewrite an
        existing one — edits and deletes travel as their own events, bound
        to the message's sender.
        """
        params = self._message_upsert_params(message)

        def _do(conn) -> bool:
            cur = conn.execute(self._MESSAGE_INSERT_IGNORE_SQL, params)
            return cur.rowcount == 1

        inserted = await self._db.transact(_do)
        if inserted:
            await self.touch_last_message(
                message.conversation_id,
                at=_iso(message.created_at),
            )
        return inserted

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

    async def list_conversations_with_sender(
        self,
        sender_user_id: str,
    ) -> list[str]:
        """Return conversation ids where ``sender_user_id`` ever
        authored a message. Feeds the §Connection-Detail visibility
        cascade (USER_REMOVED → hard-delete every conversation that
        carries any message from the deprovisioned user)."""
        rows = await self._db.fetchall(
            """
            SELECT DISTINCT conversation_id
              FROM conversation_messages
             WHERE sender_user_id=?
            """,
            (sender_user_id,),
        )
        return [r["conversation_id"] for r in rows]

    async def soft_delete_message(self, message_id: str) -> None:
        await self._db.enqueue(
            "UPDATE conversation_messages "
            "SET deleted=1, content='', media_url=NULL, "
            "    file_name=NULL, mime_type=NULL, file_size_bytes=NULL, "
            "    media_blob_id=NULL, media_sync_status=NULL "
            "WHERE id=?",
            (message_id,),
        )

    async def soft_delete_messages_by_sender(
        self,
        conversation_id: str,
        sender_user_id: str,
    ) -> int:
        """Clear every message ``sender_user_id`` wrote in ``conversation_id``.

        Same shape as :meth:`soft_delete_message`; returns how many rows it
        cleared. Used when a group member is deprovisioned by their home
        household — the group itself stays for everyone else.
        """
        return await self._db.enqueue_rowcount(
            "UPDATE conversation_messages "
            "SET deleted=1, content='', media_url=NULL, "
            "    file_name=NULL, mime_type=NULL, file_size_bytes=NULL, "
            "    media_blob_id=NULL, media_sync_status=NULL "
            "WHERE conversation_id=? AND sender_user_id=? AND deleted=0",
            (conversation_id, sender_user_id),
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

    async def find_pending_audio_transcripts(
        self,
        *,
        since_iso: str,
        limit: int = 50,
    ) -> list[ConversationMessage]:
        """Audio messages awaiting a transcript (receiver-side STT).

        Filters: ``type='audio'``, empty ``content`` (no transcript
        yet), not soft-deleted, ``created_at >= since_iso`` (the
        scheduler's 1-hour window — past that we stop retrying), and
        the sender is a **remote** user. Local users' messages are
        excluded because the sender-side STT already ran (or
        deliberately didn't) on this household; the receiver-side
        fallback exists to fill in transcripts for messages we
        received from another household whose STT couldn't supply
        one.

        ``ORDER BY created_at`` so the oldest backlog drains first.
        """
        rows = await self._db.fetchall(
            "SELECT * FROM conversation_messages "
            "WHERE type='audio' "
            "  AND content='' "
            "  AND deleted=0 "
            "  AND created_at >= ? "
            "  AND media_url IS NOT NULL "
            "  AND sender_user_id NOT IN (SELECT user_id FROM users) "
            "ORDER BY created_at ASC "
            "LIMIT ?",
            (since_iso, int(limit)),
        )
        return [m for m in (_row_to_message(d) for d in rows_to_dicts(rows)) if m]

    async def update_media_sync_status(
        self,
        *,
        message_id: str,
        status: str | None,
        media_url: str | None = None,
    ) -> None:
        """Flip ``media_sync_status`` (and optionally ``media_url``).

        Used by the receiver's ``DM_MEDIA_BLOB`` handler to swap the
        preview for the full bytes (``media_url`` updated, status
        cleared) and by ``DmMediaSyncService``'s retry-budget
        exhaustion path on the sender side (``status='failed'``,
        media_url stays pointing at the local full bytes).
        """
        if media_url is not None:
            await self._db.enqueue(
                "UPDATE conversation_messages "
                "SET media_sync_status=?, media_url=? "
                "WHERE id=?",
                (status, media_url, message_id),
            )
        else:
            await self._db.enqueue(
                "UPDATE conversation_messages SET media_sync_status=? WHERE id=?",
                (status, message_id),
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
        users table). Neither do messages a guardian block (§CP.F2) withholds
        from the member — they are never shown to them.
        """
        return int(
            await self._db.fetchval(
                f"""
            SELECT COUNT(*) FROM conversation_messages m
              LEFT JOIN users u ON u.username = ?
             WHERE m.conversation_id = ?
               AND (u.user_id IS NULL OR m.sender_user_id != u.user_id)
               AND m.sender_user_id NOT IN (
                   {guardian_block_counterparts_sql("u.user_id")}
               )
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

    # ── Delivery state (§12.5) ─────────────────────────────────────────

    async def upsert_delivery_state(
        self,
        *,
        conversation_id: str,
        message_id: str,
        user_id: str,
        state: str,
    ) -> None:
        """Record that ``user_id`` has ``delivered`` or ``read`` the message.

        ``read`` supersedes ``delivered`` — once a message is marked
        read, subsequent ``delivered`` calls are no-ops so the UI
        doesn't "downgrade" to the single tick.
        """
        if state not in ("delivered", "read"):
            raise ValueError(f"invalid delivery state: {state!r}")
        now = datetime.now(timezone.utc).isoformat()
        await self._db.enqueue(
            """
            INSERT INTO conversation_delivery_state(
                conversation_id, message_id, user_id, state, state_at
            ) VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id, message_id, user_id) DO UPDATE SET
                state = CASE
                    WHEN conversation_delivery_state.state = 'read' THEN 'read'
                    ELSE excluded.state
                END,
                state_at = CASE
                    WHEN conversation_delivery_state.state = 'read' THEN
                        conversation_delivery_state.state_at
                    ELSE excluded.state_at
                END
            """,
            (conversation_id, message_id, user_id, state, now),
        )

    async def list_delivery_states(
        self,
        conversation_id: str,
        *,
        message_ids: list[str] | None = None,
    ) -> list[dict]:
        if message_ids is not None and not message_ids:
            return []
        if message_ids is None:
            rows = await self._db.fetchall(
                "SELECT message_id, user_id, state, state_at "
                "FROM conversation_delivery_state WHERE conversation_id=?",
                (conversation_id,),
            )
        else:
            placeholders = ",".join("?" for _ in message_ids)
            rows = await self._db.fetchall(
                f"SELECT message_id, user_id, state, state_at "
                f"FROM conversation_delivery_state "
                f"WHERE conversation_id=? AND message_id IN ({placeholders})",
                (conversation_id, *message_ids),
            )
        return [
            {
                "message_id": r["message_id"],
                "user_id": r["user_id"],
                "state": r["state"],
                "state_at": r["state_at"],
            }
            for r in rows
        ]

    async def mark_conversation_read(
        self,
        *,
        conversation_id: str,
        user_id: str,
        up_to_at: str,
    ) -> int:
        """Bulk-upsert 'read' state for every non-own message created at
        or before ``up_to_at``. Returns the number of rows touched.

        Called when a client opens a conversation + the user scrolls
        through it; saves a round-trip per message.
        """
        rows = await self._db.fetchall(
            "SELECT id FROM conversation_messages "
            "WHERE conversation_id=? AND sender_user_id<>? "
            "AND created_at<=? AND COALESCE(deleted,0)=0",
            (conversation_id, user_id, up_to_at),
        )
        count = 0
        now = datetime.now(timezone.utc).isoformat()
        for row in rows:
            await self._db.enqueue(
                """
                INSERT INTO conversation_delivery_state(
                    conversation_id, message_id, user_id, state, state_at
                ) VALUES(?, ?, ?, 'read', ?)
                ON CONFLICT(conversation_id, message_id, user_id) DO UPDATE SET
                    state='read',
                    state_at=excluded.state_at
                """,
                (conversation_id, row["id"], user_id, now),
            )
            count += 1
        return count


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
        membership_version=int(row.get("membership_version") or 0),
        system_scope=(
            SystemChatScope(row["system_scope"]) if row.get("system_scope") else None
        ),
        space_id=row.get("space_id"),
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
        file_name=row.get("file_name"),
        mime_type=row.get("mime_type"),
        file_size_bytes=row.get("file_size_bytes"),
        media_blob_id=row.get("media_blob_id"),
        media_sync_status=row.get("media_sync_status"),
        reply_to_id=row.get("reply_to_id"),
        reply_to_highlight_frame_id=row.get("reply_to_highlight_frame_id"),
        reply_to_highlight_frame_snapshot=row.get("reply_to_highlight_frame_snapshot"),
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
