"""Pairing frames only a household admin can act on reach admins only."""

from __future__ import annotations

import pytest

from socialhome.domain.events import (
    AutoPairRequestIncoming,
    PairingIntroReceived,
)
from socialhome.domain.user import User
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.ws_manager import WebSocketManager
from socialhome.services.realtime_service import RealtimeService


class _FakeUserRepo:
    async def list_active(self):
        return [
            User(user_id="adm", username="adm", display_name="A", is_admin=True),
            User(user_id="kid", username="kid", display_name="K"),
        ]


class _FakeSpaceRepo:
    async def list_local_member_user_ids(self, space_id):
        return []


class _FakeWS:
    def __init__(self):
        self.sent: list[str] = []

    async def send_str(self, msg):
        self.sent.append(msg)

    @property
    def closed(self):
        return False


@pytest.fixture
async def env():
    bus = EventBus()
    ws = WebSocketManager()
    RealtimeService(
        bus, ws, user_repo=_FakeUserRepo(), space_repo=_FakeSpaceRepo()
    ).wire()
    adm, kid = _FakeWS(), _FakeWS()
    await ws.register("adm", adm)
    await ws.register("kid", kid)
    return bus, adm, kid


async def test_pairing_intro_received_goes_to_admins_only(env):
    bus, adm, kid = env
    await bus.publish(
        PairingIntroReceived(from_instance="i-a", via_instance_id="i-b", message="hi")
    )
    assert any("pairing.intro_received" in m for m in adm.sent)
    assert kid.sent == []


async def test_auto_pair_requested_goes_to_admins_only(env):
    bus, adm, kid = env
    await bus.publish(
        AutoPairRequestIncoming(
            request_id="r1",
            from_a_id="i-a",
            from_a_display="Alpha",
            via_b_id="i-b",
            via_b_display="Beta",
        )
    )
    assert any("pairing.auto_pair_requested" in m for m in adm.sent)
    assert kid.sent == []
