"""Space-chat exporter — the newest messages of a space's chat (v_55).

Streams the messages of this household's chat for the space that are not
deleted and inside the space's retention window (the whole chat when the
space keeps forever — :mod:`..window`), oldest first and page by page, so
a member household that joined (or was offline) gets the conversation, not
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

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from ..exporter import PagedExporterMixin
from ..window import SYNC_PAGE_SIZE, iter_pages, window_for_space

if TYPE_CHECKING:
    from .....domain.conversation import ConversationMessage
    from .....repositories.conversation_repo import AbstractConversationRepo
    from .....repositories.space_repo import AbstractSpaceRepo


def _iter_chat_pages(
    convos: "AbstractConversationRepo",
    conversation_id: str,
    cutoff: str | None,
    *,
    deleted: bool,
) -> AsyncIterator[list["ConversationMessage"]]:
    async def fetch(
        cursor: int | None,
    ) -> tuple[list["ConversationMessage"], int | None]:
        return await convos.list_messages_sync_page(
            conversation_id,
            deleted=deleted,
            cutoff=cutoff,
            cursor=cursor,
            limit=SYNC_PAGE_SIZE,
        )

    return iter_pages(fetch)


class ChatMessagesExporter(PagedExporterMixin):
    resource = "chat_messages"

    __slots__ = ("_convos", "_spaces")

    def __init__(
        self,
        conversation_repo: "AbstractConversationRepo",
        space_repo: "AbstractSpaceRepo",
    ) -> None:
        self._convos = conversation_repo
        self._spaces = space_repo

    async def iter_batches(self, space_id: str) -> AsyncIterator[list[dict[str, Any]]]:
        space = await self._spaces.get(space_id)
        if space is None or space.dissolved or not space.features.chat:
            return
        chat = await self._convos.get_space_chat(space_id)
        if chat is None:
            return
        cutoff = window_for_space(space).cutoff
        async for page in _iter_chat_pages(
            self._convos, chat.id, cutoff, deleted=False
        ):
            yield [
                {
                    "id": m.id,
                    "message_id": m.id,
                    "author_user_id": m.sender_user_id,
                    "content": m.content,
                    "reply_to_id": m.reply_to_id,
                    "created_at": m.created_at.isoformat(),
                }
                for m in page
                if m.type == "text" and m.content
            ]


class ChatMessagesDeletedExporter(PagedExporterMixin):
    """The space chat's deletions (``chat_messages_deleted``) inside the
    space's retention window (all of them when it keeps forever): ids and
    their authors only — never content. Tombstones of messages never held
    here included, so a deletion travels on even from a household that
    only ever saw the delete."""

    resource = "chat_messages_deleted"

    __slots__ = ("_convos", "_spaces")

    def __init__(
        self,
        conversation_repo: "AbstractConversationRepo",
        space_repo: "AbstractSpaceRepo",
    ) -> None:
        self._convos = conversation_repo
        self._spaces = space_repo

    async def iter_batches(self, space_id: str) -> AsyncIterator[list[dict[str, Any]]]:
        space = await self._spaces.get(space_id)
        if space is None or space.dissolved:
            return
        chat = await self._convos.get_space_chat(space_id)
        if chat is None:
            return
        cutoff = window_for_space(space).cutoff
        async for page in _iter_chat_pages(self._convos, chat.id, cutoff, deleted=True):
            yield [
                {"id": m.id, "message_id": m.id, "author_user_id": m.sender_user_id}
                for m in page
            ]
