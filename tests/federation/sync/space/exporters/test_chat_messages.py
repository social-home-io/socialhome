"""Tests for ``socialhome.federation.sync.space.exporters.chat_messages``."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from socialhome.domain.conversation import (
    Conversation,
    ConversationMessage,
    ConversationType,
    SystemChatScope,
)
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.federation.sync.space.exporter import ALLOWED_RESOURCES
from socialhome.federation.sync.space.exporters import ChatMessagesExporter
from socialhome.federation.sync.space.exporters.chat_messages import (
    CHAT_CATCH_UP_LIMIT,
)

_AT = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
_SPACE = Space(
    id="sp-1",
    name="S",
    owner_instance_id="host",
    owner_username="anna",
    identity_public_key="ab" * 32,
    config_sequence=0,
    features=SpaceFeatures(),
    space_type=SpaceType.PRIVATE,
    join_mode=JoinMode.INVITE_ONLY,
)


class _Spaces:
    def __init__(self, space: Space | None) -> None:
        self.space = space

    async def get(self, space_id: str) -> Space | None:
        return self.space


class _Convos:
    def __init__(self, *, chat: bool = True) -> None:
        self.chat = (
            Conversation(
                id="chat-1",
                type=ConversationType.GROUP_DM,
                created_at=_AT,
                system_scope=SystemChatScope.SPACE,
                space_id="sp-1",
            )
            if chat
            else None
        )
        self.limit: int | None = None

    async def get_space_chat(self, space_id: str) -> Conversation | None:
        assert space_id == "sp-1"
        return self.chat

    async def list_recent_live_messages(self, conversation_id, *, limit):
        assert conversation_id == "chat-1"
        self.limit = limit
        return [
            ConversationMessage(
                id="m-1",
                conversation_id="chat-1",
                sender_user_id="u-a",
                content="hi",
                created_at=_AT,
            ),
            ConversationMessage(
                id="m-2",
                conversation_id="chat-1",
                sender_user_id="u-b",
                content="yo",
                created_at=_AT,
                reply_to_id="m-1",
            ),
            # Never streamed: a non-text message (none exist today) and an
            # emptied one.
            ConversationMessage(
                id="m-3",
                conversation_id="chat-1",
                sender_user_id="u-b",
                content="{}",
                created_at=_AT,
                type="location",
            ),
            ConversationMessage(
                id="m-4",
                conversation_id="chat-1",
                sender_user_id="u-b",
                content="",
                created_at=_AT,
            ),
        ]


async def test_streams_the_recent_window_in_the_live_payload_shape():
    convos = _Convos()
    exporter = ChatMessagesExporter(convos, _Spaces(_SPACE))  # type: ignore[arg-type]
    assert exporter.resource == "chat_messages" in ALLOWED_RESOURCES
    records = await exporter.list_records("sp-1")
    assert convos.limit == CHAT_CATCH_UP_LIMIT == 500
    assert records == [
        {
            "id": "m-1",
            "message_id": "m-1",
            "author_user_id": "u-a",
            "content": "hi",
            "reply_to_id": None,
            "created_at": _AT.isoformat(),
        },
        {
            "id": "m-2",
            "message_id": "m-2",
            "author_user_id": "u-b",
            "content": "yo",
            "reply_to_id": "m-1",
            "created_at": _AT.isoformat(),
        },
    ]
    # No conversation id rides the stream.
    assert all("conversation_id" not in r for r in records)


async def test_nothing_streams_without_a_chat_or_while_it_is_off():
    assert (
        await ChatMessagesExporter(
            _Convos(chat=False),  # type: ignore[arg-type]
            _Spaces(_SPACE),  # type: ignore[arg-type]
        ).list_records("sp-1")
        == []
    )
    off = dataclasses.replace(_SPACE, features=SpaceFeatures(chat=False))
    dissolved = dataclasses.replace(_SPACE, dissolved=True)
    for space in (off, dissolved, None):
        assert (
            await ChatMessagesExporter(
                _Convos(),  # type: ignore[arg-type]
                _Spaces(space),  # type: ignore[arg-type]
            ).list_records("sp-1")
            == []
        )
