"""Direct message / group DM domain types (§5.2 / §23.47).

:class:`Conversation` models a 1:1 DM or a group DM. Messages are
:class:`ConversationMessage` records with a small type vocabulary.
:class:`MessageReaction` records per-user reactions on a message.

All types are immutable dataclasses. Mutations return new instances.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from .errors import CodedError


class ConversationType(StrEnum):
    DM = "dm"  # exactly 2 participants
    GROUP_DM = "group_dm"  # 3+ participants; may carry an optional name


class SystemChatScope(StrEnum):
    """What a *system chat* belongs to (``conversations.system_scope``).

    A system chat is a ``group_dm`` the household creates itself — never a
    person — reusing the whole group-DM machinery (messages, seats with
    their read watermark / mute / level, reactions, edit / delete). Who may
    read and write it is decided live by
    :class:`~socialhome.services.system_chat_policy.SystemChatPolicy`; it is
    kept out of the DM inbox and the DM badge. Mirrors the column CHECK.
    """

    #: Every active local user; never federated.
    HOUSEHOLD = "household"
    #: One per space (``Conversation.space_id``).
    SPACE = "space"


# Allowed ``type`` values for a :class:`ConversationMessage`.
#
# Media attachments (``image`` / ``video`` / ``file`` / ``audio``)
# carry their bytes via ``media_url`` plus the ``file_name`` /
# ``mime_type`` / ``file_size_bytes`` siblings below. Same-household
# renders straight from the local-signed URL; cross-household uses
# the preview-now-sync-later flow (a tiny preview is embedded in the
# encrypted ``DM_MESSAGE`` envelope, the full bytes follow on a
# ``DM_MEDIA_BLOB`` event).
#
# ``audio`` (voice notes, OGG/Opus) is the one media type whose
# ``content`` is *also* meaningful — it carries the STT transcript.
# Empty string until the sender's STT runs (or the receiver's local
# fallback STT runs); the bubble renders a "Transcribing…"
# placeholder while it is empty.
#
# ``transcript`` and ``location`` carry their data inside ``content``.
MESSAGE_TYPES: frozenset[str] = frozenset(
    {
        "text",
        "image",
        "video",
        "file",
        "audio",
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
    bot_enabled: bool = False  # True → HA bot-bridge may post to this DM
    #: Group conversations only: the version of the member list this
    #: household holds. Bumped by the authority household (the one the
    #: conversation id is bound to) on every membership change; a
    #: receiver applies a ``DM_GROUP_ROSTER`` only when it is newer.
    membership_version: int = 0
    #: ``None`` for a person-made DM or group; set for a system chat
    #: (:class:`SystemChatScope`).
    system_scope: SystemChatScope | None = None
    #: The space a :attr:`SystemChatScope.SPACE` chat belongs to.
    space_id: str | None = None

    @property
    def is_system(self) -> bool:
        """A system chat (household / space), not a person-made DM."""
        return self.system_scope is not None


@dataclass(slots=True, frozen=True)
class SystemChatSummary:
    """What the SPA needs to show one system chat for one viewer.

    ``enabled=False`` (the household / space turned the chat off) carries
    no conversation: the chat is hidden, its data kept.
    """

    enabled: bool
    conversation_id: str | None = None
    unread: int = 0
    notif_level: str | None = None
    #: The viewer's own mute while it is still on (UTC ISO 8601), else
    #: ``None``.
    muted_until: str | None = None
    #: The viewer's read watermark (UTC; SQLite ``datetime('now')`` shape)
    #: — anchors the thread's "New messages" divider. ``None`` = never read.
    last_read_at: str | None = None


@dataclass(slots=True, frozen=True)
class ConversationMessage:
    id: str
    conversation_id: str
    sender_user_id: str
    content: str
    created_at: datetime

    type: str = "text"
    media_url: str | None = None
    #: Original filename for ``type='file'`` (or a user-friendly label
    #: for image/video uploads). NULL for ``text`` / ``transcript`` /
    #: ``location`` and for any media without a label.
    file_name: str | None = None
    #: IANA media type — drives the receiver's render branch
    #: (``image/*`` → inline ``<img>``, ``video/*`` → ``<video>``,
    #: anything else → file pill with a glyph + filename + size).
    mime_type: str | None = None
    #: Authoritative byte count of the full-quality media (post-
    #: transcoding for image / video, raw for ``file``). Surfaces in
    #: the bubble as "1.2 MB" so the recipient knows what they're
    #: about to load on a metered connection.
    file_size_bytes: int | None = None
    #: Stable identifier shared with the follow-up ``DM_MEDIA_BLOB``
    #: event when this message rides cross-household. NULL for local
    #: messages or non-media types.
    media_blob_id: str | None = None
    #: Cross-household sync state. NULL when the message is local OR
    #: the full bytes have arrived (``media_url`` now points at the
    #: local-stored full media). ``'pending'`` = the bubble renders
    #: the preview embedded in the envelope while waiting for the
    #: blob; ``'failed'`` = sender gave up after retry-budget
    #: exhaustion.
    media_sync_status: str | None = None
    reply_to_id: str | None = None
    #: Highlight-frame reply (§Highlights). Set when the user replied to a
    #: highlight frame from the viewer; the snapshot below freezes a
    #: thumbnail + caption so the reply stays meaningful after the
    #: source frame is removed by the retention scheduler.
    reply_to_highlight_frame_id: str | None = None
    reply_to_highlight_frame_snapshot: str | None = None  # JSON
    deleted: bool = False
    edited_at: datetime | None = None

    def soft_delete(self) -> "ConversationMessage":
        return copy.replace(
            self,
            content="",
            media_url=None,
            file_name=None,
            mime_type=None,
            file_size_bytes=None,
            media_blob_id=None,
            media_sync_status=None,
            deleted=True,
        )

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
    #: Groups: the membership version at which this member was last seated
    #: (the authority ships it as each roster entry's ``since``).
    joined_version: int | None = None
    #: Groups kept by another household: the version held here when this
    #: user left — a roster not re-adding them after it can't seat them.
    left_version: int | None = None
    #: The member muted this conversation until this UTC ISO 8601 time
    #: (:data:`MUTED_FOREVER` for "until I turn it back on"). A past time
    #: reads as unmuted — see :func:`mute_active`. Local only, never
    #: federated.
    muted_until: str | None = None
    #: Group conversations: which messages ring this member —
    #: ``"all"`` (default) or ``"mentions"`` (only messages that @-mention
    #: them). One of :data:`CONVERSATION_NOTIF_LEVELS`. An active mute wins
    #: over either. Local only, never federated.
    notif_level: str = "all"


#: Per-member notification levels of a group conversation (see
#: :attr:`ConversationMember.notif_level`). Mirrors the column CHECK.
CONVERSATION_NOTIF_LEVELS: frozenset[str] = frozenset({"all", "mentions"})

#: ``muted_until`` for a mute with no end ("until I turn it back on").
MUTED_FOREVER = "9999-12-31T23:59:59+00:00"

#: The mute lengths a member can pick; ``None`` = until they unmute.
MUTE_DURATIONS: dict[str, timedelta | None] = {
    "1h": timedelta(hours=1),
    "8h": timedelta(hours=8),
    "1w": timedelta(weeks=1),
    "forever": None,
}


def mute_until_for(duration: str, *, now: datetime) -> str:
    """The ``muted_until`` value for a mute of ``duration`` starting ``now``.

    Raises :class:`ValueError` for a length not in :data:`MUTE_DURATIONS`.
    """
    if duration not in MUTE_DURATIONS:
        raise ValueError(f"duration must be one of {', '.join(MUTE_DURATIONS)}")
    delta = MUTE_DURATIONS[duration]
    if delta is None:
        return MUTED_FOREVER
    until = now.astimezone(timezone.utc) + delta
    return until.replace(microsecond=0).isoformat()


def mute_active(muted_until: str | None, *, now: datetime) -> bool:
    """Is a mute stamped ``muted_until`` still on at ``now``?

    ``None`` / unparseable / past → ``False``: an expired mute simply
    reads as unmuted, so no scheduler has to clear it. A naive timestamp
    is taken as UTC.
    """
    if not muted_until:
        return False
    try:
        until = datetime.fromisoformat(muted_until)
    except ValueError:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until > now


@dataclass(slots=True, frozen=True)
class RemoteConversationMember:
    """One participant row for a remote user in a federated conversation."""

    conversation_id: str
    instance_id: str
    remote_username: str
    joined_at: str
    history_visible_from: str | None = None
    #: Set when the seat came from a group roster: the member's global
    #: ``user_id`` and the name the authority household ships for them.
    #: A household that never paired with this member's household has no
    #: ``remote_users`` row for them, so the seat is what binds their
    #: messages to them. ``None`` on 1:1 seats (``remote_users`` answers).
    user_id: str | None = None
    display_name: str | None = None
    #: The membership version at which this seat was last filled.
    joined_version: int | None = None


@dataclass(slots=True, frozen=True)
class GroupRosterChange:
    """What applying one group member-list snapshot changed here.

    Returned by ``AbstractConversationRepo.apply_group_roster``. ``None``
    is returned instead when the snapshot was not newer than the version
    already held (a reordered or replayed roster), so a caller never
    mistakes a stale snapshot for "nothing changed".
    """

    conversation_id: str
    version: int
    #: The conversation row did not exist before this snapshot.
    created: bool
    #: Local usernames seated (new, or back after leaving) by it.
    added_local: tuple[str, ...]
    #: Local usernames it took out of the conversation.
    removed_local: tuple[str, ...]
    #: ``(instance_id, remote_username)`` seats it removed.
    removed_remote: tuple[tuple[str, str], ...]


@dataclass(slots=True, frozen=True)
class GroupRosterMember:
    """One person on a group conversation's member list, as the wire names them.

    The shape of a ``DM_GROUP_ROSTER`` ``members`` entry (v_37): the
    member's global ``user_id``, their home household's ``instance_id``,
    their username there and the display name the authority ships.
    """

    user_id: str
    instance_id: str
    username: str
    display_name: str
    #: The version at which the authority last seated this member; ``None``
    #: for a seat older than the field (omitted on the wire).
    since: int | None = None

    def to_wire(self) -> dict[str, str | int]:
        wire: dict[str, str | int] = {
            "user_id": self.user_id,
            "instance_id": self.instance_id,
            "username": self.username,
            "display_name": self.display_name,
        }
        if self.since is not None:
            wire["since"] = self.since
        return wire


class DmSelfError(CodedError, ValueError):
    """A direct message to yourself."""

    status = 422
    code = "DM_SELF"
    detail = "cannot DM yourself"


class GroupTooSmallError(CodedError, ValueError):
    """A group conversation needs at least ``min`` people, creator included."""

    status = 422
    code = "GROUP_TOO_SMALL"

    def __init__(self, minimum: int = 3) -> None:
        super().__init__(
            f"group DM requires at least {minimum} participants",
            params={"min": minimum},
        )


class DmTooLongError(CodedError, ValueError):
    """A message over the per-message length cap (``params.max``)."""

    status = 422
    code = "DM_TOO_LONG"

    def __init__(self, maximum: int) -> None:
        super().__init__(
            f"message content exceeds {maximum} chars",
            params={"max": maximum},
        )
