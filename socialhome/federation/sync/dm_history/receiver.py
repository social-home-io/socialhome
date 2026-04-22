"""Requester side of the DM history sync."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ....domain.conversation import MESSAGE_TYPES, ConversationMessage
from ....domain.events import DmHistorySyncComplete
from ....domain.federation import FederationEventType

if TYPE_CHECKING:
    from ....domain.federation import FederationEvent
    from ....federation.federation_service import FederationService
    from ....infrastructure.event_bus import EventBus
    from ....repositories.conversation_repo import AbstractConversationRepo


log = logging.getLogger(__name__)


class DmHistoryReceiver:
    """Persists inbound DM history chunks and emits the sync-complete event.

    :meth:`save_message` is upsert-by-id (``ON CONFLICT(id) DO UPDATE``),
    so replayed or overlapping chunks are harmless.
    """

    __slots__ = ("_conversation_repo", "_bus", "_counts", "_federation")

    def __init__(
        self,
        *,
        conversation_repo: "AbstractConversationRepo",
        bus: "EventBus",
        federation_service: "FederationService | None" = None,
    ) -> None:
        self._conversation_repo = conversation_repo
        self._bus = bus
        self._federation = federation_service
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

        saved = 0
        for raw in raw_messages:
            msg = _dict_to_message(raw, conversation_id)
            if msg is None:
                continue
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
        """Publish :class:`DmHistorySyncComplete`."""
        payload = event.payload or {}
        conversation_id = str(payload.get("conversation_id") or "")
        if not conversation_id:
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
