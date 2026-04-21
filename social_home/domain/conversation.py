"""Direct message / group DM domain types (§5.2 / §23.47).

:class:`Conversation` models a 1:1 DM or a group DM. Messages are
:class:`ConversationMessage` records with a small type vocabulary.
:class:`MessageReaction` records per-user reactions on a message.

All types are immutable dataclasses. Mutations return new instances.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum


class ConversationType(StrEnum):
    DM = "dm"  # exactly 2 participants
    GROUP_DM = "group_dm"  # 3+ participants; may carry an optional name


# Allowed ``type`` values for a :class:`ConversationMessage`.
MESSAGE_TYPES: frozenset[str] = frozenset(
    {
        "text",
        "image",
        "video",
        "transcript",
        "location",
    }
)


@dataclass(slots=True, frozen=True)
class Conversation:
    id: str
    type: ConversationType
    created_at: datetime

    name: str | None = None  # set for group DMs, None for 1:1
    last_message_at: datetime | None = None
    notify_enabled: bool = False  # True → exposed as HA notify entity


@dataclass(slots=True, frozen=True)
class ConversationMessage:
    id: str
    conversation_id: str
    sender_user_id: str
    content: str
    created_at: datetime

    type: str = "text"
    media_url: str | None = None
    reply_to_id: str | None = None
    deleted: bool = False
    edited_at: datetime | None = None

    def soft_delete(self) -> "ConversationMessage":
        return copy.replace(self, content="", media_url=None, deleted=True)

    def edit(
        self, new_content: str, *, now: datetime | None = None
    ) -> "ConversationMessage":
        return copy.replace(
            self,
            content=new_content,
            edited_at=now or datetime.now(timezone.utc),
        )


@dataclass(slots=True, frozen=True)
class MessageReaction:
    message_id: str
    user_id: str
    emoji: str
    reacted_at: datetime


@dataclass(slots=True, frozen=True)
class ConversationMember:
    """One participant row of a :class:`Conversation` (local users only)."""

    conversation_id: str
    username: str  # local username (FK to users)
    joined_at: str
    last_read_at: str | None = None
    history_visible_from: str | None = None
    # Soft-delete for 1:1 DMs — set when a participant leaves. None = active.
    deleted_at: str | None = None


@dataclass(slots=True, frozen=True)
class RemoteConversationMember:
    """One participant row for a remote user in a federated conversation."""

    conversation_id: str
    instance_id: str
    remote_username: str
    joined_at: str
    history_visible_from: str | None = None
