"""Requester side of the DM history sync."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ....domain.conversation import MESSAGE_TYPES, ConversationMessage
from ....domain.events import DmHistorySyncComplete
from ....domain.federation import FederationEventType
from ...dm_scope import DmScope, refuse

if TYPE_CHECKING:
    from ....domain.federation import FederationEvent
    from ....federation.federation_service import FederationService
    from ....infrastructure.event_bus import EventBus
    from ....repositories.conversation_repo import AbstractConversationRepo
    from ....repositories.user_repo import AbstractUserRepo


log = logging.getLogger(__name__)


class DmHistoryReceiver:
    """Persists inbound DM history chunks and emits the sync-complete event.

    History only fills gaps: a chunk is taken from a household seated in
    the conversation, each message must be its own seated user's — or, for
    a group, any member's on another household when the chunk comes from
    the group's authority (a newly added household catches up from it) —
    and it is inserted when absent (:meth:`insert_message_if_absent`). A message
    already here is updated from the chunk (its sender's later edit or
    delete) only when the stored row has that same sender in that same
    conversation — never anyone else's message.
    """

    __slots__ = (
        "_conversation_repo",
        "_bus",
        "_counts",
        "_federation",
        "_dm_scope",
    )

    def __init__(
        self,
        *,
        conversation_repo: "AbstractConversationRepo",
        user_repo: "AbstractUserRepo",
        bus: "EventBus",
        federation_service: "FederationService | None" = None,
    ) -> None:
        self._conversation_repo = conversation_repo
        self._bus = bus
        self._federation = federation_service
        self._dm_scope = DmScope(
            conversation_repo=conversation_repo,
            user_repo=user_repo,
        )
        # (from_instance, conversation_id) → chunks seen so far
        self._counts: dict[tuple[str, str], int] = {}

    def attach_federation(self, federation_service) -> None:
        """Wire :class:`FederationService` so the receiver can send
        :data:`DM_HISTORY_CHUNK_ACK` frames (§12)."""
        self._federation = federation_service

    async def handle_chunk(self, event: "FederationEvent") -> int:
        """Persist every message in the chunk. Returns the count saved."""
        payload = event.payload or {}
        conversation_id = str(payload.get("conversation_id") or "")
        raw_messages = payload.get("messages") or []
        if not conversation_id or not isinstance(raw_messages, list):
            log.debug("DM_HISTORY_CHUNK malformed: %s", payload)
            return 0
        if not await self._dm_scope.seated(event, conversation_id):
            refuse(
                event,
                "sender holds no seat in the conversation",
                conversation=conversation_id,
            )
            return 0

        saved = 0
        for raw in raw_messages:
            msg = _dict_to_message(raw, conversation_id)
            if msg is None:
                continue
            own = await self._dm_scope.speaks_for(
                event, conversation_id, msg.sender_user_id
            )
            if not own and not await self._dm_scope.relayed_by_authority(
                event, conversation_id, msg.sender_user_id
            ):
                refuse(
                    event,
                    "message is not a seated user's of the sending household",
                    conversation=conversation_id,
                    message=msg.id,
                )
                continue
            if await self._conversation_repo.insert_message_if_absent(msg):
                saved += 1
                continue
            if not own:
                # A row the authority relays for another household's member
                # only fills a gap: it never overwrites, un-deletes or rolls
                # back what that member's own household delivered.
                continue
            # Already here: the catch-up copy may carry the sender's own later
            # edit or delete — applied only onto that same sender's row in
            # that same conversation, never onto anyone else's message.
            stored = await self._conversation_repo.get_message(msg.id)
            if (
                stored is not None
                and stored.sender_user_id == msg.sender_user_id
                and stored.conversation_id == conversation_id
            ):
                await self._conversation_repo.save_message(msg)
                saved += 1

        key = (event.from_instance, conversation_id)
        self._counts[key] = self._counts.get(key, 0) + 1

        # Ack the chunk so the provider knows we persisted it (§12).
        chunk_index = payload.get("chunk_index")
        if (
            self._federation is not None
            and isinstance(chunk_index, int)
            and chunk_index >= 0
        ):
            try:
                await self._federation.send_event(
                    to_instance_id=event.from_instance,
                    event_type=FederationEventType.DM_HISTORY_CHUNK_ACK,
                    payload={
                        "conversation_id": conversation_id,
                        "chunk_index": chunk_index,
                    },
                )
            except Exception as exc:  # pragma: no cover
                log.debug("DM_HISTORY_CHUNK_ACK send failed: %s", exc)
        return saved

    async def handle_complete(self, event: "FederationEvent") -> None:
        """Publish :class:`DmHistorySyncComplete` — for a seated sender only."""
        payload = event.payload or {}
        conversation_id = str(payload.get("conversation_id") or "")
        if not conversation_id:
            return
        if not await self._dm_scope.seated(event, conversation_id):
            refuse(
                event,
                "sender holds no seat in the conversation",
                conversation=conversation_id,
            )
            return
        key = (event.from_instance, conversation_id)
        chunks = self._counts.pop(key, int(payload.get("chunks_sent") or 0))
        await self._bus.publish(
            DmHistorySyncComplete(
                conversation_id=conversation_id,
                from_instance=event.from_instance,
                chunks_received=chunks,
            )
        )


def _dict_to_message(raw: dict, conversation_id: str) -> ConversationMessage | None:
    msg_id = str(raw.get("id") or "")
    sender_user_id = str(raw.get("sender_user_id") or "")
    if not msg_id or not sender_user_id:
        return None
    msg_type = str(raw.get("type") or "text")
    if msg_type not in MESSAGE_TYPES:
        msg_type = "text"
    return ConversationMessage(
        id=msg_id,
        conversation_id=conversation_id,
        sender_user_id=sender_user_id,
        content=str(raw.get("content") or ""),
        created_at=_parse_iso(raw.get("created_at")),
        type=msg_type,
        media_url=raw.get("media_url"),
        reply_to_id=raw.get("reply_to_id"),
        deleted=bool(raw.get("deleted") or False),
        edited_at=_parse_iso(raw.get("edited_at")) if raw.get("edited_at") else None,
    )


def _parse_iso(value) -> datetime:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)
