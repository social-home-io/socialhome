"""Provider side of the DM history sync (§12)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ....domain.federation import FederationEventType
from ....services.visibility import VisibilityMixin
from ...dm_scope import DmScope, refuse

if TYPE_CHECKING:
    from ....domain.federation import FederationEvent
    from ....repositories.conversation_repo import AbstractConversationRepo
    from ....repositories.peer_user_visibility_repo import (
        AbstractPeerUserVisibilityRepo,
    )
    from ....repositories.user_repo import AbstractUserRepo
    from ...federation_service import FederationService


log = logging.getLogger(__name__)


#: Messages per DM_HISTORY_CHUNK frame. Keeps each envelope well under the
#: 64 KB outbox row limit even for verbose media URLs.
CHUNK_SIZE: int = 50

#: Safety cap on total messages streamed per request — avoids a peer
#: asking for five years of history at once and pinning the provider.
MAX_MESSAGES_PER_REQUEST: int = 5_000


class DmHistoryProvider(VisibilityMixin):
    """Answers :data:`FederationEventType.DM_HISTORY_REQUEST` events.

    Streams missed messages back to the requester in order, then sends a
    terminal :data:`FederationEventType.DM_HISTORY_COMPLETE`. Each chunk
    includes a monotonic ``chunk_index``; when the receiver sends a
    :data:`DM_HISTORY_CHUNK_ACK` the provider records the ack so an
    operator / test harness can verify delivery without reading the DB.

    If ``visibility_repo`` and ``user_repo`` are supplied, the provider checks
    whether any local participant of the requested conversation has been hidden
    from the requesting peer. When the check fires, the chunk stream is
    suppressed and only :data:`DM_HISTORY_COMPLETE` (with ``chunks_sent=0``)
    is sent so the requester's catch-up state machine terminates cleanly.
    """

    __slots__ = (
        "_conversation_repo",
        "_federation",
        "_acks",
        "_user_repo",
        "_dm_scope",
    )

    def __init__(
        self,
        *,
        conversation_repo: "AbstractConversationRepo",
        federation_service: "FederationService",
        user_repo: "AbstractUserRepo",
        visibility_repo: "AbstractPeerUserVisibilityRepo | None" = None,
    ) -> None:
        self._conversation_repo = conversation_repo
        self._federation = federation_service
        self._user_repo = user_repo
        self._dm_scope = DmScope(
            conversation_repo=conversation_repo,
            user_repo=user_repo,
        )
        self._visibility_repo = visibility_repo
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
        carries ``is_last=True`` when there are zero new messages). Returns
        0 when a visibility gate suppresses the stream — only
        :data:`DM_HISTORY_COMPLETE` with ``chunks_sent=0`` is emitted so the
        requester's state machine terminates cleanly.
        """
        payload = event.payload or {}
        conversation_id = str(payload.get("conversation_id") or "")
        since_iso = payload.get("since")
        if not conversation_id:
            log.debug("DM_HISTORY_REQUEST missing conversation_id")
            return 0
        # History goes only to a household seated in the conversation.
        # Nothing is sent otherwise — not even DM_HISTORY_COMPLETE, which
        # would confirm the conversation exists.
        if not await self._dm_scope.seated(event, conversation_id):
            refuse(
                event,
                "requester holds no seat in the conversation",
                conversation=conversation_id,
            )
            return 0

        # Visibility gate: if any local participant is hidden from the peer,
        # suppress the chunk stream entirely.
        if self._visibility_repo is not None:
            hidden = await self.hidden_for_peer(event.from_instance)
            if hidden:
                members = await self._conversation_repo.list_members(conversation_id)
                local_user_ids: set[str] = set()
                for member in members:
                    user = await self._user_repo.get(member.username)
                    if user is not None:
                        local_user_ids.add(user.user_id)
                if local_user_ids & hidden:
                    await self._federation.send_event(
                        to_instance_id=event.from_instance,
                        event_type=FederationEventType.DM_HISTORY_COMPLETE,
                        payload={
                            "conversation_id": conversation_id,
                            "chunks_sent": 0,
                        },
                    )
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
