"""Provider side of the DM history sync (§12)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ....domain.federation import FederationEventType

if TYPE_CHECKING:
    from ....domain.federation import FederationEvent
    from ....repositories.conversation_repo import AbstractConversationRepo
    from ...federation_service import FederationService


log = logging.getLogger(__name__)


#: Messages per DM_HISTORY_CHUNK frame. Keeps each envelope well under the
#: 64 KB outbox row limit even for verbose media URLs.
CHUNK_SIZE: int = 50

#: Safety cap on total messages streamed per request — avoids a peer
#: asking for five years of history at once and pinning the provider.
MAX_MESSAGES_PER_REQUEST: int = 5_000


class DmHistoryProvider:
    """Answers :data:`FederationEventType.DM_HISTORY_REQUEST` events.

    Streams missed messages back to the requester in order, then sends a
    terminal :data:`FederationEventType.DM_HISTORY_COMPLETE`. Each chunk
    includes a monotonic ``chunk_index``; when the receiver sends a
    :data:`DM_HISTORY_CHUNK_ACK` the provider records the ack so an
    operator / test harness can verify delivery without reading the DB.
    """

    __slots__ = ("_conversation_repo", "_federation", "_acks")

    def __init__(
        self,
        *,
        conversation_repo: "AbstractConversationRepo",
        federation_service: "FederationService",
    ) -> None:
        self._conversation_repo = conversation_repo
        self._federation = federation_service
        # (from_instance, conversation_id) → highest chunk_index ack'd.
        self._acks: dict[tuple[str, str], int] = {}

    async def handle_ack(self, event: "FederationEvent") -> None:
        """Record a DM_HISTORY_CHUNK_ACK receipt (spec §12)."""
        payload = event.payload or {}
        conversation_id = str(payload.get("conversation_id") or "")
        try:
            chunk_index = int(payload.get("chunk_index") or -1)
        except TypeError, ValueError:
            chunk_index = -1
        if not conversation_id or chunk_index < 0:
            return
        key = (event.from_instance, conversation_id)
        self._acks[key] = max(self._acks.get(key, -1), chunk_index)

    def last_ack(
        self,
        *,
        from_instance: str,
        conversation_id: str,
    ) -> int:
        return self._acks.get((from_instance, conversation_id), -1)

    async def handle_request(self, event: "FederationEvent") -> int:
        """Stream messages newer than ``since`` for one conversation.

        Returns the number of chunks sent (including the empty one that
        carries ``is_last=True`` when there are zero new messages).
        """
        payload = event.payload or {}
        conversation_id = str(payload.get("conversation_id") or "")
        since_iso = payload.get("since")
        if not conversation_id:
            log.debug("DM_HISTORY_REQUEST missing conversation_id")
            return 0

        messages = await self._conversation_repo.list_messages_since(
            conversation_id,
            since_iso if since_iso else None,
            limit=MAX_MESSAGES_PER_REQUEST,
        )

        chunks_sent = 0
        for i in range(0, max(1, len(messages)), CHUNK_SIZE):
            batch = messages[i : i + CHUNK_SIZE]
            is_last = (i + CHUNK_SIZE) >= len(messages)
            await self._federation.send_event(
                to_instance_id=event.from_instance,
                event_type=FederationEventType.DM_HISTORY_CHUNK,
                payload={
                    "conversation_id": conversation_id,
                    "chunk_index": chunks_sent,
                    "messages": [_message_to_dict(m) for m in batch],
                    "is_last": is_last,
                },
            )
            chunks_sent += 1
            if not messages:
                # No messages → one empty chunk so the receiver emits its
                # completion event even in the zero-new-messages case.
                break

        await self._federation.send_event(
            to_instance_id=event.from_instance,
            event_type=FederationEventType.DM_HISTORY_COMPLETE,
            payload={
                "conversation_id": conversation_id,
                "chunks_sent": chunks_sent,
            },
        )
        return chunks_sent


def _message_to_dict(msg) -> dict:
    return {
        "id": msg.id,
        "conversation_id": msg.conversation_id,
        "sender_user_id": msg.sender_user_id,
        "content": msg.content,
        "type": msg.type,
        "media_url": msg.media_url,
        "reply_to_id": msg.reply_to_id,
        "deleted": bool(msg.deleted),
        "edited_at": msg.edited_at.isoformat() if msg.edited_at else None,
        "created_at": msg.created_at.isoformat(),
    }
