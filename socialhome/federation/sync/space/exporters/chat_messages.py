"""Space-chat exporter — the newest messages of a space's chat (v_55).

Streams the last :data:`CHAT_CATCH_UP_LIMIT` messages of this household's
chat for the space that are not deleted, oldest first, so a member
household that joined (or was offline) gets the recent conversation, not
only what is said after it arrived. Each record carries the fields of the
live ``SPACE_CHAT_MESSAGE_CREATED`` payload; the receiver runs every record
through the same create rule as the live event (owner-bound id, the author
a writer the provider speaks for, idempotent insert —
:meth:`~socialhome.services.federation_inbound.space_chat
.SpaceChatInboundHandlers.apply_sync_records`). Like every resource, the
chunk is encrypted under the space content key.

Nothing streams while the space's chat is off here. WHO may receive the
resource at all is the provider's call (``SpaceSyncProvider._exporter_for``):
only a household holding a writer seat, at v_55 or later — a follower-only
household never gets the chat by catch-up either.

Deleted messages are never streamed as messages. They travel as their own
resource, ``chat_messages_deleted`` (:class:`ChatMessagesDeletedExporter`),
streamed BEFORE the messages: a household that missed a delete (offline,
or the delete overtook its create) applies it, and a tombstone keeps the id
from ever coming back — so a provider that missed the delete cannot
re-spread the message either (the joiner refuses it once the deletion
landed; the provider itself refuses it from then on). An edit that happened
before the stream is in the record's content.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .....repositories.conversation_repo import AbstractConversationRepo
    from .....repositories.space_repo import AbstractSpaceRepo

#: How many recent messages a catch-up carries.
CHAT_CATCH_UP_LIMIT = 500


class ChatMessagesExporter:
    resource = "chat_messages"

    __slots__ = ("_convos", "_spaces")

    def __init__(
        self,
        conversation_repo: "AbstractConversationRepo",
        space_repo: "AbstractSpaceRepo",
    ) -> None:
        self._convos = conversation_repo
        self._spaces = space_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        space = await self._spaces.get(space_id)
        if space is None or space.dissolved or not space.features.chat:
            return []
        chat = await self._convos.get_space_chat(space_id)
        if chat is None:
            return []
        return [
            {
                "id": m.id,
                "message_id": m.id,
                "author_user_id": m.sender_user_id,
                "content": m.content,
                "reply_to_id": m.reply_to_id,
                "created_at": m.created_at.isoformat(),
            }
            for m in await self._convos.list_recent_live_messages(
                chat.id, limit=CHAT_CATCH_UP_LIMIT
            )
            if m.type == "text" and m.content
        ]


#: How many recent deletions a catch-up carries.
CHAT_TOMBSTONE_LIMIT = 2000


class ChatMessagesDeletedExporter:
    """The space chat's recent deletions (``chat_messages_deleted``): ids
    and their authors only — never content. Tombstones of messages never
    held here included, so a deletion travels on even from a household
    that only ever saw the delete."""

    resource = "chat_messages_deleted"

    __slots__ = ("_convos", "_spaces")

    def __init__(
        self,
        conversation_repo: "AbstractConversationRepo",
        space_repo: "AbstractSpaceRepo",
    ) -> None:
        self._convos = conversation_repo
        self._spaces = space_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        space = await self._spaces.get(space_id)
        if space is None or space.dissolved:
            return []
        chat = await self._convos.get_space_chat(space_id)
        if chat is None:
            return []
        return [
            {"id": m.id, "message_id": m.id, "author_user_id": m.sender_user_id}
            for m in await self._convos.list_recent_deleted_messages(
                chat.id, limit=CHAT_TOMBSTONE_LIMIT
            )
        ]
