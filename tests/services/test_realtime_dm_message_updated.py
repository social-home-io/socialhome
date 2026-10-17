"""Tests for the ``dm.message_updated`` WS frame.

Exercises :meth:`RealtimeService._on_dm_message_updated` —
the bridge from the in-process :class:`DmMessageUpdated` event to the
WS broadcast that patches the open thread tabs.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from socialhome.domain.events import DmMessageUpdated
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.realtime_service import RealtimeService

pytestmark = pytest.mark.asyncio


class _CapturingWs:
    """Stand-in for :class:`WebSocketManager` — records every broadcast."""

    __slots__ = ("calls",)

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def broadcast_to_user(self, user_id: str, payload: dict) -> None:
        self.calls.append((user_id, payload))

    async def broadcast_to_all(self, payload: dict) -> None:  # pragma: no cover
        self.calls.append(("*", payload))


@pytest.fixture
def realtime():
    """Realtime service wired to a capturing WS + bus.

    Test stubs for ``user_repo`` / ``space_repo`` cover the constructor
    requirement — the ``DmMessageUpdated`` path doesn't touch either,
    so simple sentinels are enough.
    """
    bus = EventBus()
    ws = _CapturingWs()
    svc = RealtimeService(
        bus=bus,
        ws=ws,
        user_repo=object(),
        space_repo=object(),
    )
    svc.wire()
    return svc, bus, ws


async def test_dm_message_updated_broadcasts_to_sender_and_recipients(realtime):
    """Every distinct user_id receives the patched frame once."""
    _svc, bus, ws = realtime
    edited_at = datetime.now(UTC)
    await bus.publish(
        DmMessageUpdated(
            conversation_id="conv-1",
            message_id="m-1",
            sender_user_id="u-anna",
            recipient_user_ids=("u-bob", "u-carol"),
            content="hello world",
            edited_at=edited_at,
        )
    )
    delivered = {uid for uid, _ in ws.calls}
    assert delivered == {"u-anna", "u-bob", "u-carol"}


async def test_dm_message_updated_frame_shape(realtime):
    """Frame carries id + content + edited_at, no ``message`` blob."""
    _svc, bus, ws = realtime
    edited_at = datetime.now(UTC)
    await bus.publish(
        DmMessageUpdated(
            conversation_id="conv-1",
            message_id="m-1",
            sender_user_id="u-anna",
            recipient_user_ids=("u-bob",),
            content="patched transcript",
            edited_at=edited_at,
        )
    )
    payload = ws.calls[0][1]
    assert payload["type"] == "dm.message_updated"
    assert payload["conversation_id"] == "conv-1"
    assert payload["message_id"] == "m-1"
    assert payload["content"] == "patched transcript"
    assert payload["edited_at"] == edited_at.isoformat()


async def test_dm_message_updated_dedupes_when_sender_in_recipient_list(realtime):
    """Defensive: sender appearing in ``recipient_user_ids`` doesn't double-send."""
    _svc, bus, ws = realtime
    edited_at = datetime.now(UTC)
    await bus.publish(
        DmMessageUpdated(
            conversation_id="conv-1",
            message_id="m-1",
            sender_user_id="u-anna",
            recipient_user_ids=("u-anna", "u-bob"),  # sender twice
            content="x",
            edited_at=edited_at,
        )
    )
    # u-anna gets exactly one frame, u-bob gets one frame → 2 total.
    assert len(ws.calls) == 2
    assert {uid for uid, _ in ws.calls} == {"u-anna", "u-bob"}


async def test_dm_message_updated_skips_blank_user_ids(realtime):
    """Empty-string sender / recipient (a defensive seed) is filtered."""
    _svc, bus, ws = realtime
    edited_at = datetime.now(UTC)
    await bus.publish(
        DmMessageUpdated(
            conversation_id="conv-1",
            message_id="m-1",
            sender_user_id="",
            recipient_user_ids=("", "u-bob"),
            content="x",
            edited_at=edited_at,
        )
    )
    assert [uid for uid, _ in ws.calls] == ["u-bob"]
