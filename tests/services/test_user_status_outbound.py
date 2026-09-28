"""UserStatusOutbound — fan a local UserStatusChanged out as USER_STATUS_UPDATED."""

from __future__ import annotations

import pytest

from socialhome.domain.events import UserStatusChanged
from socialhome.domain.federation import FederationEventType
from socialhome.domain.user import User, UserStatus
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.user_status_outbound import UserStatusOutbound


class _FakeFederationService:
    own_instance_id = "own-inst"

    def __init__(self) -> None:
        self.sent: list[tuple[str, FederationEventType, dict]] = []

    async def send_event(self, *, to_instance_id, event_type, payload):
        self.sent.append((to_instance_id, event_type, payload))


class _Peer:
    def __init__(self, instance_id: str) -> None:
        self.id = instance_id


class _FakeFedRepo:
    async def list_social_instances(self):
        return [_Peer("peer-a"), _Peer("peer-b"), _Peer("own-inst")]


class _FakeUserRepo:
    """Only ``u-local`` is homed here (the ``users`` table is local-only)."""

    async def get_by_user_id(self, user_id: str):
        if user_id == "u-local":
            return User(user_id="u-local", username="anna", display_name="Anna")
        return None


class _FakeVisibilityRepo:
    async def hidden_user_ids_for_peer(self, peer: str) -> frozenset[str]:
        return frozenset({"u-local"}) if peer == "peer-b" else frozenset()


@pytest.fixture
def env():
    bus = EventBus()
    fed = _FakeFederationService()
    UserStatusOutbound(
        bus=bus,
        federation_service=fed,  # type: ignore[arg-type]
        federation_repo=_FakeFedRepo(),  # type: ignore[arg-type]
        user_repo=_FakeUserRepo(),  # type: ignore[arg-type]
    ).wire()
    return bus, fed


async def test_local_status_goes_to_every_confirmed_peer(env):
    bus, fed = env
    status = UserStatus(
        emoji="🌴", text="On leave", expires_at="2026-10-01T00:00:00+00:00"
    )
    await bus.publish(UserStatusChanged(user_id="u-local", status=status))
    assert fed.sent == [
        (
            peer,
            FederationEventType.USER_STATUS_UPDATED,
            {
                "user_id": "u-local",
                "emoji": "🌴",
                "text": "On leave",
                "expires_at": "2026-10-01T00:00:00+00:00",
            },
        )
        for peer in ("peer-a", "peer-b")
    ]


async def test_cleared_status_sends_status_cleared(env):
    bus, fed = env
    await bus.publish(UserStatusChanged(user_id="u-local", status=None))
    assert {p for p, _, _ in fed.sent} == {"peer-a", "peer-b"}
    assert all(
        payload == {"user_id": "u-local", "status_cleared": True}
        for _, _, payload in fed.sent
    )


async def test_remote_users_status_is_not_echoed(env):
    """The inbound handler re-publishes a peer's status on the local bus;
    re-sending it would have us speak for another household's user."""
    bus, fed = env
    await bus.publish(
        UserStatusChanged(user_id="u-remote", status=UserStatus(text="hi"))
    )
    assert fed.sent == []


async def test_user_hidden_from_a_peer_is_skipped_for_that_peer():
    bus = EventBus()
    fed = _FakeFederationService()
    UserStatusOutbound(
        bus=bus,
        federation_service=fed,  # type: ignore[arg-type]
        federation_repo=_FakeFedRepo(),  # type: ignore[arg-type]
        user_repo=_FakeUserRepo(),  # type: ignore[arg-type]
        visibility_repo=_FakeVisibilityRepo(),  # type: ignore[arg-type]
    ).wire()
    await bus.publish(UserStatusChanged(user_id="u-local", status=UserStatus(text="x")))
    assert [p for p, _, _ in fed.sent] == ["peer-a"]
