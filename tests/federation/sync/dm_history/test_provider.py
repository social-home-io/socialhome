"""Unit tests for :class:`DmHistoryProvider`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


from social_home.domain.conversation import ConversationMessage
from social_home.domain.federation import FederationEventType
from social_home.federation.sync.dm_history.provider import (
    CHUNK_SIZE,
    DmHistoryProvider,
)


class _FakeFederation:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_event(self, *, to_instance_id, event_type, payload, space_id=None):
        self.sent.append(
            {
                "to": to_instance_id,
                "type": event_type,
                "payload": payload,
            }
        )


class _FakeConvRepo:
    def __init__(self, messages):
        self._messages = messages
        self.last_since: str | None = None
        self.last_limit: int | None = None

    async def list_messages_since(self, conversation_id, since_iso, *, limit=500):
        self.last_since = since_iso
        self.last_limit = limit
        if since_iso is None:
            return list(self._messages)
        return [m for m in self._messages if m.created_at.isoformat() > since_iso]


def _msg(i: int, at: datetime) -> ConversationMessage:
    return ConversationMessage(
        id=f"m-{i}",
        conversation_id="c-1",
        sender_user_id="u-1",
        content=f"msg {i}",
        created_at=at,
    )


def _event(from_instance: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        event_type=FederationEventType.DM_HISTORY_REQUEST,
        from_instance=from_instance,
        payload=payload,
    )


async def test_streams_messages_in_order_and_emits_complete():
    now = datetime(2026, 4, 1, tzinfo=timezone.utc)
    messages = [_msg(i, now + timedelta(minutes=i)) for i in range(3)]
    fed = _FakeFederation()
    provider = DmHistoryProvider(
        conversation_repo=_FakeConvRepo(messages),
        federation_service=fed,
    )
    count = await provider.handle_request(
        _event(
            "peer-a",
            {"conversation_id": "c-1", "since": ""},
        )
    )
    chunks = [s for s in fed.sent if s["type"] == FederationEventType.DM_HISTORY_CHUNK]
    completes = [
        s for s in fed.sent if s["type"] == FederationEventType.DM_HISTORY_COMPLETE
    ]
    assert count == 1
    assert len(chunks) == 1
    assert [m["id"] for m in chunks[0]["payload"]["messages"]] == ["m-0", "m-1", "m-2"]
    assert chunks[0]["payload"]["is_last"] is True
    assert len(completes) == 1
    assert completes[0]["payload"]["chunks_sent"] == 1


async def test_respects_since_cursor():
    now = datetime(2026, 4, 1, tzinfo=timezone.utc)
    messages = [_msg(i, now + timedelta(minutes=i)) for i in range(3)]
    fed = _FakeFederation()
    repo = _FakeConvRepo(messages)
    provider = DmHistoryProvider(
        conversation_repo=repo,
        federation_service=fed,
    )
    since = (now + timedelta(minutes=1)).isoformat()
    await provider.handle_request(
        _event(
            "peer-a",
            {"conversation_id": "c-1", "since": since},
        )
    )
    assert repo.last_since == since
    chunks = [s for s in fed.sent if s["type"] == FederationEventType.DM_HISTORY_CHUNK]
    assert [m["id"] for m in chunks[0]["payload"]["messages"]] == ["m-2"]


async def test_large_history_is_split_into_multiple_chunks():
    now = datetime(2026, 4, 1, tzinfo=timezone.utc)
    messages = [_msg(i, now + timedelta(seconds=i)) for i in range(CHUNK_SIZE + 5)]
    fed = _FakeFederation()
    provider = DmHistoryProvider(
        conversation_repo=_FakeConvRepo(messages),
        federation_service=fed,
    )
    await provider.handle_request(
        _event(
            "peer-a",
            {"conversation_id": "c-1", "since": ""},
        )
    )
    chunks = [s for s in fed.sent if s["type"] == FederationEventType.DM_HISTORY_CHUNK]
    assert len(chunks) == 2
    # last chunk flagged
    assert chunks[0]["payload"]["is_last"] is False
    assert chunks[-1]["payload"]["is_last"] is True


async def test_empty_history_still_sends_complete():
    fed = _FakeFederation()
    provider = DmHistoryProvider(
        conversation_repo=_FakeConvRepo([]),
        federation_service=fed,
    )
    await provider.handle_request(
        _event(
            "peer-a",
            {"conversation_id": "c-1", "since": ""},
        )
    )
    completes = [
        s for s in fed.sent if s["type"] == FederationEventType.DM_HISTORY_COMPLETE
    ]
    assert len(completes) == 1


async def test_missing_conversation_id_drops():
    fed = _FakeFederation()
    provider = DmHistoryProvider(
        conversation_repo=_FakeConvRepo([]),
        federation_service=fed,
    )
    count = await provider.handle_request(
        _event(
            "peer-a",
            {"conversation_id": "", "since": ""},
        )
    )
    assert count == 0
    assert fed.sent == []
