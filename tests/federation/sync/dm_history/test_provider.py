"""Unit tests for :class:`DmHistoryProvider`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from socialhome.domain.conversation import ConversationMember, ConversationMessage
from socialhome.domain.federation import FederationEventType
from socialhome.domain.user import User
from socialhome.federation.sync.dm_history.provider import (
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
    def __init__(self, messages, members: list | None = None, seats=("peer-a",)):
        self._messages = messages
        self._members: list = members or []
        #: Households holding a remote seat in the conversation.
        self._seats = seats
        self.last_since: str | None = None
        self.last_limit: int | None = None

    async def list_messages_since(self, conversation_id, since_iso, *, limit=500):
        self.last_since = since_iso
        self.last_limit = limit
        if since_iso is None:
            return list(self._messages)
        return [m for m in self._messages if m.created_at.isoformat() > since_iso]

    async def list_members(self, conversation_id: str) -> list:
        return list(self._members)

    async def get(self, conversation_id):
        # Plain DMs only: no system chat (``DmScope`` asks).
        return None

    async def list_remote_members(self, conversation_id: str) -> list:
        return [
            SimpleNamespace(instance_id=i, remote_username="x") for i in self._seats
        ]


class _FakeUserRepo:
    def __init__(self, users: dict[str, User]) -> None:
        # username -> User mapping
        self._users = users

    async def get(self, username: str) -> User | None:
        return self._users.get(username)


class _FakeVisibilityRepo:
    def __init__(self, hidden: frozenset[str]) -> None:
        self._hidden = hidden

    async def hidden_user_ids_for_peer(self, peer_id: str) -> frozenset[str]:
        return self._hidden


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
        user_repo=_FakeUserRepo({}),
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
        user_repo=_FakeUserRepo({}),
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
        user_repo=_FakeUserRepo({}),
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
        user_repo=_FakeUserRepo({}),
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
        user_repo=_FakeUserRepo({}),
    )
    count = await provider.handle_request(
        _event(
            "peer-a",
            {"conversation_id": "", "since": ""},
        )
    )
    assert count == 0
    assert fed.sent == []


async def test_history_chunks_suppressed_when_local_participant_hidden_from_peer():
    """When the local participant is hidden from the requesting peer, no CHUNK
    envelopes are sent but DM_HISTORY_COMPLETE still fires with chunks_sent=0
    so the requester's catch-up state machine terminates cleanly."""
    now = datetime(2026, 4, 1, tzinfo=timezone.utc)
    messages = [_msg(i, now + timedelta(minutes=i)) for i in range(3)]

    local_user = User(
        user_id="uid-alice",
        username="alice",
        display_name="Alice",
    )
    member = ConversationMember(
        conversation_id="c-1",
        username="alice",
        joined_at="2026-01-01T00:00:00+00:00",
    )

    fed = _FakeFederation()
    provider = DmHistoryProvider(
        conversation_repo=_FakeConvRepo(messages, members=[member]),
        federation_service=fed,
        user_repo=_FakeUserRepo({"alice": local_user}),
        visibility_repo=_FakeVisibilityRepo(frozenset({"uid-alice"})),
    )
    count = await provider.handle_request(
        _event("peer-a", {"conversation_id": "c-1", "since": ""})
    )

    chunks = [s for s in fed.sent if s["type"] == FederationEventType.DM_HISTORY_CHUNK]
    completes = [
        s for s in fed.sent if s["type"] == FederationEventType.DM_HISTORY_COMPLETE
    ]
    assert count == 0
    assert len(chunks) == 0
    assert len(completes) == 1
    assert completes[0]["payload"]["chunks_sent"] == 0


async def test_history_streams_when_no_visibility_repo_back_compat():
    """With visibility_repo=None the original streaming
    behaviour is preserved — no regressions for existing wiring."""
    now = datetime(2026, 4, 1, tzinfo=timezone.utc)
    messages = [_msg(i, now + timedelta(minutes=i)) for i in range(2)]
    fed = _FakeFederation()
    provider = DmHistoryProvider(
        conversation_repo=_FakeConvRepo(messages),
        federation_service=fed,
        user_repo=_FakeUserRepo({}),
        visibility_repo=None,
    )
    count = await provider.handle_request(
        _event("peer-a", {"conversation_id": "c-1", "since": ""})
    )
    chunks = [s for s in fed.sent if s["type"] == FederationEventType.DM_HISTORY_CHUNK]
    assert count == 1
    assert len(chunks) == 1


async def test_no_history_for_a_household_without_a_seat():
    """A peer that holds no seat in the conversation gets nothing back."""
    now = datetime(2026, 4, 1, tzinfo=timezone.utc)
    fed = _FakeFederation()
    provider = DmHistoryProvider(
        conversation_repo=_FakeConvRepo([_msg(0, now)], seats=("peer-b",)),
        federation_service=fed,
        user_repo=_FakeUserRepo({}),
    )
    count = await provider.handle_request(
        _event("peer-a", {"conversation_id": "c-1", "since": ""})
    )
    assert count == 0
    assert fed.sent == []
