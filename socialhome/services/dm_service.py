"""DM (direct message) service — 1:1 and group conversations (§23.47).

Orchestrates :class:`AbstractConversationRepo` for conversation lifecycle,
message CRUD, reactions, and read tracking.

**Privacy rules (§25.3):**

* Message content never appears in push notification bodies.
* ``sanitise_for_api`` is applied at the route layer, not here — the
  service returns full domain objects.

**Permissions:**

* Only conversation members can send messages, react, or mark read.
* Any member of a group DM can add a new participant.
* Only the creator of a group DM can rename it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..domain.conversation import (
    Conversation,
    ConversationMember,
    ConversationMessage,
    ConversationType,
    MESSAGE_TYPES,
)
from ..domain.events import DmMessageCreated
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus
from ..repositories.conversation_repo import AbstractConversationRepo
from ..repositories.user_repo import AbstractUserRepo

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.dm_routing_repo import AbstractDmRoutingRepo
    from ..repositories.federation_repo import AbstractFederationRepo


log = logging.getLogger(__name__)


#: Per-message length cap (§23.47). Households don't need novella-length
#: chat messages; the cap also bounds the search-index row size and the
#: notification fan-out cost.
MAX_DM_LENGTH: int = 1000


class DmService:
    """Conversation + message CRUD for household DMs."""

    __slots__ = (
        "_convos",
        "_users",
        "_bus",
        "_federation",
        "_federation_repo",
        "_dm_routing_repo",
        "_own_instance_id",
    )

    def __init__(
        self,
        conversation_repo: AbstractConversationRepo,
        user_repo: AbstractUserRepo,
        bus: EventBus,
        *,
        federation_service: "FederationService | None" = None,
        federation_repo: "AbstractFederationRepo | None" = None,
        dm_routing_repo: "AbstractDmRoutingRepo | None" = None,
        own_instance_id: str = "",
    ) -> None:
        self._convos = conversation_repo
        self._users = user_repo
        self._bus = bus
        self._federation = federation_service
        self._federation_repo = federation_repo
        self._dm_routing_repo = dm_routing_repo
        self._own_instance_id = own_instance_id

    def attach_federation(
        self,
        federation_service: "FederationService",
        federation_repo: "AbstractFederationRepo",
        own_instance_id: str,
    ) -> None:
        """Wire federation after construction (breaks the DM ↔ federation cycle)."""
        self._federation = federation_service
        self._federation_repo = federation_repo
        self._own_instance_id = own_instance_id

    # ── Conversations ──────────────────────────────────────────────────

    async def create_dm(
        self,
        *,
        creator_username: str,
        other_username: str,
    ) -> Conversation:
        """Start a 1:1 DM. Idempotent if one already exists between these
        two participants — returns the existing conversation.
        """
        creator = await self._require_user(creator_username)
        other = await self._require_user(other_username)
        if creator.username == other.username:
            raise ValueError("cannot DM yourself")

        # Check for existing 1:1 between these two
        existing = await self._convos.list_for_user(creator_username)
        for conv in existing:
            if conv.type is not ConversationType.DM:
                continue
            members = await self._convos.list_members(conv.id)
            usernames = {m.username for m in members}
            if usernames == {creator.username, other.username}:
                return conv

        conv = Conversation(
            id=uuid.uuid4().hex,
            type=ConversationType.DM,
            created_at=datetime.now(timezone.utc),
        )
        await self._convos.create(conv)
        now = datetime.now(timezone.utc).isoformat()
        await self._convos.add_member(
            ConversationMember(
                conversation_id=conv.id, username=creator.username, joined_at=now
            )
        )
        await self._convos.add_member(
            ConversationMember(
                conversation_id=conv.id, username=other.username, joined_at=now
            )
        )
        return conv

    async def create_group_dm(
        self,
        *,
        creator_username: str,
        member_usernames: list[str],
        name: str | None = None,
    ) -> Conversation:
        """Start a group DM (3+ participants)."""
        creator = await self._require_user(creator_username)
        all_names = {creator.username} | set(member_usernames)
        if len(all_names) < 3:
            raise ValueError("group DM requires at least 3 participants")
        for uname in member_usernames:
            await self._require_user(uname)

        conv = Conversation(
            id=uuid.uuid4().hex,
            type=ConversationType.GROUP_DM,
            name=name.strip() if name else None,
            created_at=datetime.now(timezone.utc),
        )
        await self._convos.create(conv)
        now = datetime.now(timezone.utc).isoformat()
        for uname in sorted(all_names):
            await self._convos.add_member(
                ConversationMember(
                    conversation_id=conv.id, username=uname, joined_at=now
                )
            )
        return conv

    async def add_group_member(
        self,
        conversation_id: str,
        *,
        actor_username: str,
        new_username: str,
    ) -> None:
        """Any member of a group DM can add a new participant."""
        conv = await self._require_conversation(conversation_id)
        if conv.type is not ConversationType.GROUP_DM:
            raise ValueError("cannot add members to a 1:1 DM")
        await self._require_membership(conversation_id, actor_username)
        await self._require_user(new_username)
        await self._convos.add_member(
            ConversationMember(
                conversation_id=conversation_id,
                username=new_username,
                joined_at=datetime.now(timezone.utc).isoformat(),
            )
        )

    async def list_conversations(self, username: str) -> list[Conversation]:
        return await self._convos.list_for_user(username)

    async def get_conversation(self, conversation_id: str) -> Conversation:
        return await self._require_conversation(conversation_id)

    # ── Messages ───────────────────────────────────────────────────────

    async def send_message(
        self,
        conversation_id: str,
        *,
        sender_username: str,
        content: str,
        type: str = "text",
        media_url: str | None = None,
        reply_to_id: str | None = None,
    ) -> ConversationMessage:
        """Send a message. ``sender_username`` must be a member.

        Content is stored verbatim — sanitisation is the route layer's
        responsibility.
        """
        await self._require_conversation(conversation_id)
        await self._require_membership(conversation_id, sender_username)
        sender = await self._require_user(sender_username)
        if type not in MESSAGE_TYPES:
            raise ValueError(f"invalid message type {type!r}")
        if not content and type == "text":
            raise ValueError("message content must not be empty")
        if len(content) > MAX_DM_LENGTH:
            raise ValueError(f"message content exceeds {MAX_DM_LENGTH} chars")

        msg = ConversationMessage(
            id=uuid.uuid4().hex,
            conversation_id=conversation_id,
            sender_user_id=sender.user_id,
            content=content,
            type=type,
            media_url=media_url,
            reply_to_id=reply_to_id,
            created_at=datetime.now(timezone.utc),
        )
        await self._convos.save_message(msg)

        # Fan-out: every member except the sender is a push recipient.
        # ConversationMember stores usernames, not user_ids — resolve
        # to user_ids for the PushService which keys on user_id.
        recipients: list[str] = []
        for m in await self._convos.list_members(conversation_id):
            if m.username == sender_username:
                continue
            u = await self._users.get(m.username)
            if u is not None:
                recipients.append(u.user_id)
        await self._bus.publish(
            DmMessageCreated(
                conversation_id=conversation_id,
                message_id=msg.id,
                sender_user_id=sender.user_id,
                sender_display_name=sender.display_name,
                recipient_user_ids=tuple(recipients),
                content=content,
            )
        )
        # Stamp a monotonic sender_seq on the envelope when the
        # routing repo is wired, so recipients can run §12.5 gap
        # detection. Absent repo → legacy behaviour (no seq field).
        seq: int | None = None
        if self._dm_routing_repo is not None:
            seq = await self._dm_routing_repo.next_sender_seq(
                conversation_id=conversation_id,
                sender_user_id=sender.user_id,
            )
        payload: dict = {
            "conversation_id": conversation_id,
            "message_id": msg.id,
            "sender_user_id": sender.user_id,
            "sender_display_name": sender.display_name,
            "type": type,
            "content": content,
            "media_url": media_url,
            "reply_to_id": reply_to_id,
            "occurred_at": msg.created_at.isoformat(),
            "recipient_user_ids": recipients,
        }
        if seq is not None:
            payload["sender_seq"] = seq
        await self._fan_to_remote(
            conversation_id=conversation_id,
            event_type=FederationEventType.DM_MESSAGE,
            payload=payload,
        )
        return msg

    async def edit_message(
        self,
        message_id: str,
        *,
        editor_username: str,
        new_content: str,
    ) -> None:
        msg = await self._require_message(message_id)
        editor = await self._require_user(editor_username)
        if msg.sender_user_id != editor.user_id:
            raise PermissionError("only the sender can edit a message")
        if not new_content:
            raise ValueError("content must not be empty")
        if len(new_content) > MAX_DM_LENGTH:
            raise ValueError(f"message content exceeds {MAX_DM_LENGTH} chars")
        await self._convos.edit_message(message_id, new_content)
        # Receiver upserts on message_id (save_message ON CONFLICT UPDATE),
        # so a re-send of DM_MESSAGE with updated content + edited_at is
        # all the peer needs to reflect the edit.
        await self._fan_to_remote(
            conversation_id=msg.conversation_id,
            event_type=FederationEventType.DM_MESSAGE,
            payload={
                "conversation_id": msg.conversation_id,
                "message_id": msg.id,
                "sender_user_id": msg.sender_user_id,
                "sender_display_name": editor.display_name,
                "type": msg.type,
                "content": new_content,
                "media_url": msg.media_url,
                "reply_to_id": msg.reply_to_id,
                "occurred_at": msg.created_at.isoformat(),
                "edited_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def delete_message(
        self,
        message_id: str,
        *,
        actor_username: str,
    ) -> None:
        msg = await self._require_message(message_id)
        actor = await self._require_user(actor_username)
        if msg.sender_user_id != actor.user_id:
            raise PermissionError("only the sender can delete a message")
        await self._convos.soft_delete_message(message_id)
        await self._fan_to_remote(
            conversation_id=msg.conversation_id,
            event_type=FederationEventType.DM_MESSAGE_DELETED,
            payload={
                "conversation_id": msg.conversation_id,
                "message_id": msg.id,
            },
        )

    async def list_messages(
        self,
        conversation_id: str,
        *,
        reader_username: str,
        before: str | None = None,
        limit: int = 50,
    ) -> list[ConversationMessage]:
        await self._require_membership(conversation_id, reader_username)
        limit = max(1, min(int(limit), 100))
        return await self._convos.list_messages(
            conversation_id,
            before=before,
            limit=limit,
        )

    # ── Read tracking ──────────────────────────────────────────────────

    async def mark_read(
        self,
        conversation_id: str,
        *,
        username: str,
    ) -> int:
        """Mark every message in the conversation read for ``username``.

        Updates the watermark (`set_last_read`) for unread counts AND
        bulk-upserts ``conversation_delivery_state`` rows so other
        participants see read-receipt ticks. Returns the number of
        messages that flipped to ``read``.
        """
        await self._require_membership(conversation_id, username)
        # Pass Python-format ISO timestamp so it compares correctly with
        # message created_at (also Python ISO). SQLite's datetime('now')
        # omits the 'T' and timezone suffix, causing string-comparison
        # mismatches against Python isoformat() values.
        now = datetime.now(timezone.utc).isoformat()
        await self._convos.set_last_read(conversation_id, username, at=now)
        user = await self._require_user(username)
        return await self._convos.mark_conversation_read(
            conversation_id=conversation_id,
            user_id=user.user_id,
            up_to_at=now,
        )

    async def mark_delivered(
        self,
        conversation_id: str,
        *,
        message_id: str,
        username: str,
    ) -> None:
        """Mark ``message_id`` as delivered to ``username``.

        Called when the client receives a message via WebSocket or the
        GET /messages endpoint. ``read`` already supersedes
        ``delivered`` (handled in the repo), so calling after mark_read
        is a no-op.
        """
        await self._require_membership(conversation_id, username)
        user = await self._require_user(username)
        await self._convos.upsert_delivery_state(
            conversation_id=conversation_id,
            message_id=message_id,
            user_id=user.user_id,
            state="delivered",
        )

    async def list_delivery_states(
        self,
        conversation_id: str,
        *,
        username: str,
        message_ids: list[str] | None = None,
    ) -> list[dict]:
        await self._require_membership(conversation_id, username)
        return await self._convos.list_delivery_states(
            conversation_id,
            message_ids=message_ids,
        )

    async def list_open_gaps(
        self,
        conversation_id: str,
        *,
        username: str,
    ) -> list[dict]:
        """§12.5 — sequence holes detected for this conversation.

        Members-only. Returns ``[]`` when the routing repo wasn't wired
        or nothing is flagged.
        """
        await self._require_membership(conversation_id, username)
        if self._dm_routing_repo is None:
            return []
        return await self._dm_routing_repo.list_open_gaps(conversation_id)

    async def count_unread(
        self,
        conversation_id: str,
        *,
        username: str,
    ) -> int:
        await self._require_membership(conversation_id, username)
        return await self._convos.count_unread(conversation_id, username)

    # ── Reactions ──────────────────────────────────────────────────────

    async def add_reaction(
        self,
        message_id: str,
        *,
        user_id: str,
        emoji: str,
    ) -> None:
        # Verifies existence — raises if the message was purged.
        msg = await self._require_message(message_id)
        # membership check would require finding the conversation of the
        # message and the username of the user_id. For v1 we trust the
        # caller (route layer already verified membership).
        clean = emoji.strip()
        await self._convos.add_reaction(message_id, user_id, clean)
        await self._fan_to_remote(
            conversation_id=msg.conversation_id,
            event_type=FederationEventType.DM_MESSAGE_REACTION,
            payload={
                "conversation_id": msg.conversation_id,
                "message_id": msg.id,
                "user_id": user_id,
                "emoji": clean,
                "action": "add",
            },
        )

    async def remove_reaction(
        self,
        message_id: str,
        *,
        user_id: str,
        emoji: str,
    ) -> None:
        msg = await self._require_message(message_id)
        clean = emoji.strip()
        await self._convos.remove_reaction(message_id, user_id, clean)
        await self._fan_to_remote(
            conversation_id=msg.conversation_id,
            event_type=FederationEventType.DM_MESSAGE_REACTION,
            payload={
                "conversation_id": msg.conversation_id,
                "message_id": msg.id,
                "user_id": user_id,
                "emoji": clean,
                "action": "remove",
            },
        )

    # ── Leave ──────────────────────────────────────────────────────────

    async def leave(
        self,
        conversation_id: str,
        *,
        username: str,
    ) -> None:
        """Soft-leave: sets ``deleted_at`` on the member row.

        Applies to both 1:1 DMs and group DMs (§23.47c). The user can be
        re-invited; their messages stay visible to remaining members. A
        background sweeper hard-deletes the conversation once every member
        has left.
        """
        await self._require_membership(conversation_id, username)
        await self._require_conversation(conversation_id)
        await self._convos.soft_leave(conversation_id, username)

    # ── Internal helpers ───────────────────────────────────────────────

    async def _require_user(self, username: str):
        user = await self._users.get(username)
        if user is None:
            raise KeyError(f"user {username!r} not found")
        return user

    async def _require_conversation(
        self,
        conversation_id: str,
    ) -> Conversation:
        conv = await self._convos.get(conversation_id)
        if conv is None:
            raise KeyError(f"conversation {conversation_id!r} not found")
        return conv

    async def _require_membership(
        self,
        conversation_id: str,
        username: str,
    ) -> ConversationMember:
        members = await self._convos.list_members(conversation_id)
        for m in members:
            if m.username == username and m.deleted_at is None:
                return m
        raise PermissionError(f"user {username!r} is not a member of this conversation")

    async def _require_message(
        self,
        message_id: str,
    ) -> ConversationMessage:
        msg = await self._convos.get_message(message_id)
        if msg is None:
            raise KeyError(f"message {message_id!r} not found")
        return msg

    # ── Federation fan-out ─────────────────────────────────────────────

    async def _fan_to_remote(
        self,
        *,
        conversation_id: str,
        event_type: FederationEventType,
        payload: dict,
    ) -> None:
        """Send ``event_type`` to every remote instance with a member in
        ``conversation_id``.

        Direct paired peers receive the event via
        :meth:`FederationService.send_event`. Peers we only know
        transitively (no :class:`RemoteInstance` row) are skipped — the
        browser/client drives multi-hop relay (§12.5) where the content
        is E2E-encrypted before leaving the device, and the DM history
        sync picks up anything the peer missed when they reconnect.
        """
        if self._federation is None:
            return
        try:
            remote_members = await self._convos.list_remote_members(
                conversation_id,
            )
        except Exception:  # pragma: no cover
            return
        seen: set[str] = set()
        for rm in remote_members:
            inst = getattr(rm, "instance_id", None)
            if not inst or inst == self._own_instance_id or inst in seen:
                continue
            seen.add(inst)
            if not await self._peer_is_confirmed(inst):
                log.debug(
                    "dm fan-out skipped: peer %s not confirmed",
                    inst,
                )
                continue
            try:
                await self._federation.send_event(
                    to_instance_id=inst,
                    event_type=event_type,
                    payload=payload,
                )
            except Exception as exc:  # pragma: no cover
                log.debug(
                    "dm fan-out failed to %s (%s): %s",
                    inst,
                    event_type.value,
                    exc,
                )

    async def _peer_is_confirmed(self, instance_id: str) -> bool:
        """Is ``instance_id`` a directly-paired CONFIRMED peer?

        Unconfirmed / transitive-only peers take the multi-hop relay
        path, which is browser-driven in v1 (§12.5).
        """
        if self._federation_repo is None:
            # Without a federation_repo we can't tell — be permissive so
            # tests that only wire the federation service keep working.
            return True
        try:
            instance = await self._federation_repo.get_instance(instance_id)
        except Exception:  # pragma: no cover
            return False
        if instance is None:
            return False
        status = getattr(instance, "status", None)
        if status is None:
            return False
        return getattr(status, "value", str(status)) == "confirmed"
