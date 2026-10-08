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
from socialhome.federation.sync.space.exporter import REMOVAL_RESOURCES, RESOURCE_ORDER
from socialhome.federation.sync.space.exporters import (
    ChatMessagesDeletedExporter,
    ChatMessagesExporter,
)
from socialhome.federation.sync.space.window import SYNC_PAGE_SIZE

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
        #: Every page asked for: (deleted, cutoff, cursor, limit).
        self.asked: list[tuple] = []

    async def list_messages_sync_page(
        self, conversation_id, *, deleted, cutoff, cursor, limit
    ):
        assert conversation_id == "chat-1"
        self.asked.append((deleted, cutoff, cursor, limit))
        rows = self._deleted() if deleted else self._live()
        return rows, None

    def _deleted(self):
        return [
            ConversationMessage(
                id="m-gone",
                conversation_id="chat-1",
                sender_user_id="u-a",
                content="",
                created_at=_AT,
                deleted=True,
            ),
            ConversationMessage(
                id="m-tomb",
                conversation_id="chat-1",
                sender_user_id="u-b",
                content="",
                created_at=_AT,
                type="tombstone",
                deleted=True,
            ),
        ]

    async def get_space_chat(self, space_id: str) -> Conversation | None:
        assert space_id == "sp-1"
        return self.chat

    def _live(self):
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
    # No retention on the space: the whole chat, paged — no fixed count.
    assert convos.asked == [(False, None, None, SYNC_PAGE_SIZE)]
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


async def test_deletions_stream_ids_and_authors_only_before_the_messages():
    convos = _Convos()
    exporter = ChatMessagesDeletedExporter(convos, _Spaces(_SPACE))  # type: ignore[arg-type]
    assert exporter.resource == "chat_messages_deleted" in REMOVAL_RESOURCES
    assert RESOURCE_ORDER.index("chat_messages_deleted") < RESOURCE_ORDER.index(
        "chat_messages"
    )
    assert await exporter.list_records("sp-1") == [
        {"id": "m-gone", "message_id": "m-gone", "author_user_id": "u-a"},
        {"id": "m-tomb", "message_id": "m-tomb", "author_user_id": "u-b"},
    ]
    assert convos.asked == [(True, None, None, SYNC_PAGE_SIZE)]
    # A deletion streams even while the chat is off (a removal must land).
    off = dataclasses.replace(_SPACE, features=SpaceFeatures(chat=False))
    assert await ChatMessagesDeletedExporter(
        _Convos(),  # type: ignore[arg-type]
        _Spaces(off),  # type: ignore[arg-type]
    ).list_records("sp-1")
    for space, convos2 in ((None, _Convos()), (_SPACE, _Convos(chat=False))):
        assert (
            await ChatMessagesDeletedExporter(
                convos2,  # type: ignore[arg-type]
                _Spaces(space),  # type: ignore[arg-type]
            ).list_records("sp-1")
            == []
        )


class _PagedConvos(_Convos):
    """A chat of ``n`` live messages served ``SYNC_PAGE_SIZE`` at a time."""

    def __init__(self, n: int) -> None:
        super().__init__()
        self.rows = [
            ConversationMessage(
                id=f"m-{i}",
                conversation_id="chat-1",
                sender_user_id="u-a",
                content=f"msg {i}",
                created_at=_AT,
            )
            for i in range(n)
        ]

    async def list_messages_sync_page(
        self, conversation_id, *, deleted, cutoff, cursor, limit
    ):
        self.asked.append((deleted, cutoff, cursor, limit))
        start = cursor or 0
        page = self.rows[start : start + limit]
        nxt = start + limit if len(page) == limit else None
        return page, nxt


async def test_a_long_chat_streams_whole_page_by_page():
    convos = _PagedConvos(SYNC_PAGE_SIZE * 2 + 7)
    exporter = ChatMessagesExporter(convos, _Spaces(_SPACE))  # type: ignore[arg-type]
    pages = [page async for page in exporter.iter_batches("sp-1")]
    assert [len(p) for p in pages] == [SYNC_PAGE_SIZE, SYNC_PAGE_SIZE, 7]
    assert [r["id"] for p in pages for r in p] == [m.id for m in convos.rows]
    assert [c for _, _, c, _ in convos.asked] == [None, SYNC_PAGE_SIZE, 400]


async def test_a_space_with_retention_streams_its_window():
    convos = _Convos()
    kept = dataclasses.replace(_SPACE, retention_days=7)
    await ChatMessagesExporter(convos, _Spaces(kept)).list_records("sp-1")  # type: ignore[arg-type]
    await ChatMessagesDeletedExporter(convos, _Spaces(kept)).list_records("sp-1")  # type: ignore[arg-type]
    cutoffs = [c for _, c, _, _ in convos.asked]
    assert len(cutoffs) == 2 and all(c is not None for c in cutoffs)
    expected = datetime.now(timezone.utc).replace(tzinfo=None)
    for c in cutoffs:
        age = expected - datetime.fromisoformat(c)
        assert 6.99 < age.total_seconds() / 86400 < 7.01
