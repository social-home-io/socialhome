"""Unit tests for :class:`DmHistoryReceiver`."""

from __future__ import annotations

from types import SimpleNamespace


from socialhome.domain.events import DmHistorySyncComplete
from socialhome.domain.federation import FederationEventType
from socialhome.federation.sync.dm_history.receiver import DmHistoryReceiver
from socialhome.infrastructure.event_bus import EventBus


class _FakeConvRepo:
    """Conversation ``c-1`` seats ``x`` of household ``peer-a``."""

    def __init__(self):
        self.saved: list = []
        self.ids: set[str] = set()

    async def insert_message_if_absent(self, message) -> bool:
        # Mimic the insert-only semantics of the real repo.
        if message.id in self.ids:
            return False
        self.saved.append(message)
        self.ids.add(message.id)
        return True

    async def get_message(self, message_id):
        return next((m for m in self.saved if m.id == message_id), None)

    async def save_message(self, message):
        """Upsert: a re-sent row replaces the stored one (edits / deletes)."""
        self.saved = [m for m in self.saved if m.id != message.id] + [message]
        return message

    async def list_remote_members(self, conversation_id):
        if conversation_id != "c-1":
            return []
        return [SimpleNamespace(instance_id="peer-a", remote_username="x")]


class _FakeUserRepo:
    """``u-x`` is user ``x`` of ``peer-a``; ``u-local`` is ours."""

    async def get_instance_for_user(self, user_id):
        return {"u-x": "peer-a", "u-local": "us"}.get(user_id)

    async def get_remote(self, user_id):
        if user_id != "u-x":
            return None
        return SimpleNamespace(instance_id="peer-a", remote_username="x")


def _event(event_type, from_instance, payload):
    return SimpleNamespace(
        event_type=event_type,
        from_instance=from_instance,
        payload=payload,
    )


def _raw_message(i: int, ts: str) -> dict:
    return {
        "id": f"m-{i}",
        "sender_user_id": "u-x",
        "content": f"msg {i}",
        "type": "text",
        "created_at": ts,
    }


async def test_chunk_persists_each_message():
    repo = _FakeConvRepo()
    bus = EventBus()
    r = DmHistoryReceiver(conversation_repo=repo, user_repo=_FakeUserRepo(), bus=bus)
    saved = await r.handle_chunk(
        _event(
            FederationEventType.DM_HISTORY_CHUNK,
            "peer-a",
            {
                "conversation_id": "c-1",
                "messages": [
                    _raw_message(0, "2026-04-01T00:00:00+00:00"),
                    _raw_message(1, "2026-04-01T00:01:00+00:00"),
                ],
                "is_last": False,
            },
        )
    )
    assert saved == 2
    assert {m.id for m in repo.saved} == {"m-0", "m-1"}


async def test_duplicate_chunk_is_idempotent():
    repo = _FakeConvRepo()
    bus = EventBus()
    r = DmHistoryReceiver(conversation_repo=repo, user_repo=_FakeUserRepo(), bus=bus)
    payload = {
        "conversation_id": "c-1",
        "messages": [_raw_message(0, "2026-04-01T00:00:00+00:00")],
        "is_last": True,
    }
    await r.handle_chunk(
        _event(
            FederationEventType.DM_HISTORY_CHUNK,
            "peer-a",
            payload,
        )
    )
    await r.handle_chunk(
        _event(
            FederationEventType.DM_HISTORY_CHUNK,
            "peer-a",
            payload,
        )
    )
    assert len(repo.saved) == 1


async def test_complete_publishes_domain_event():
    bus = EventBus()
    received: list[DmHistorySyncComplete] = []

    async def _on_complete(event: DmHistorySyncComplete) -> None:
        received.append(event)

    bus.subscribe(DmHistorySyncComplete, _on_complete)
    r = DmHistoryReceiver(
        conversation_repo=_FakeConvRepo(), user_repo=_FakeUserRepo(), bus=bus
    )
    # Prime the chunk counter so the event reports 1.
    await r.handle_chunk(
        _event(
            FederationEventType.DM_HISTORY_CHUNK,
            "peer-a",
            {"conversation_id": "c-1", "messages": [], "is_last": True},
        )
    )
    await r.handle_complete(
        _event(
            FederationEventType.DM_HISTORY_COMPLETE,
            "peer-a",
            {"conversation_id": "c-1", "chunks_sent": 1},
        )
    )
    assert len(received) == 1
    assert received[0].conversation_id == "c-1"
    assert received[0].from_instance == "peer-a"
    assert received[0].chunks_received == 1


async def test_chunk_missing_conversation_id_drops():
    repo = _FakeConvRepo()
    bus = EventBus()
    r = DmHistoryReceiver(conversation_repo=repo, user_repo=_FakeUserRepo(), bus=bus)
    saved = await r.handle_chunk(
        _event(
            FederationEventType.DM_HISTORY_CHUNK,
            "peer-a",
            {
                "conversation_id": "",
                "messages": [_raw_message(0, "2026-04-01T00:00:00+00:00")],
            },
        )
    )
    assert saved == 0
    assert repo.saved == []


async def test_chunk_skips_record_without_id_or_sender():
    repo = _FakeConvRepo()
    bus = EventBus()
    r = DmHistoryReceiver(conversation_repo=repo, user_repo=_FakeUserRepo(), bus=bus)
    await r.handle_chunk(
        _event(
            FederationEventType.DM_HISTORY_CHUNK,
            "peer-a",
            {
                "conversation_id": "c-1",
                "messages": [
                    {"id": "", "sender_user_id": "u-x", "content": "x"},
                    {"id": "m-1", "sender_user_id": "", "content": "x"},
                    _raw_message(2, "2026-04-01T00:00:00+00:00"),
                ],
            },
        )
    )
    assert {m.id for m in repo.saved} == {"m-2"}


async def test_chunk_skips_messages_not_written_by_a_seated_sender_user():
    """A local member's or an unknown user's message is not taken."""
    repo = _FakeConvRepo()
    r = DmHistoryReceiver(
        conversation_repo=repo, user_repo=_FakeUserRepo(), bus=EventBus()
    )
    saved = await r.handle_chunk(
        _event(
            FederationEventType.DM_HISTORY_CHUNK,
            "peer-a",
            {
                "conversation_id": "c-1",
                "messages": [
                    {
                        **_raw_message(0, "2026-04-01T00:00:00+00:00"),
                        "sender_user_id": "u-local",
                    },
                    {
                        **_raw_message(1, "2026-04-01T00:00:00+00:00"),
                        "sender_user_id": "u-ghost",
                    },
                    _raw_message(2, "2026-04-01T00:00:00+00:00"),
                ],
            },
        )
    )
    assert saved == 1
    assert {m.id for m in repo.saved} == {"m-2"}


async def test_chunk_and_complete_from_an_unseated_household_are_ignored():
    bus = EventBus()
    received: list = []

    async def _on_complete(event) -> None:
        received.append(event)

    bus.subscribe(DmHistorySyncComplete, _on_complete)
    repo = _FakeConvRepo()
    r = DmHistoryReceiver(conversation_repo=repo, user_repo=_FakeUserRepo(), bus=bus)
    saved = await r.handle_chunk(
        _event(
            FederationEventType.DM_HISTORY_CHUNK,
            "peer-b",
            {
                "conversation_id": "c-1",
                "messages": [_raw_message(0, "2026-04-01T00:00:00+00:00")],
            },
        )
    )
    await r.handle_complete(
        _event(
            FederationEventType.DM_HISTORY_COMPLETE,
            "peer-b",
            {"conversation_id": "c-1", "chunks_sent": 1},
        )
    )
    assert saved == 0
    assert repo.saved == []
    assert received == []
