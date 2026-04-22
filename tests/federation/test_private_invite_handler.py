"""Inbound coverage for :class:`PrivateSpaceInviteHandler`.

Complements :mod:`test_private_invite_zero_leak` (outbound) by
exercising every inbound event type: invite received, accept, decline,
member removed, plus the missing-field skip branches.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from social_home.domain.events import (
    RemoteSpaceInviteAccepted,
    RemoteSpaceInviteDeclined,
    RemoteSpaceInviteReceived,
    RemoteSpaceMemberRemoved,
)
from social_home.federation.private_invite_handler import PrivateSpaceInviteHandler
from social_home.infrastructure.event_bus import EventBus


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list = []

    async def publish(self, event) -> None:
        self.events.append(event)


def _event(event_type: str, payload: dict, *, from_instance: str = "peer-1"):
    # FederationEvent is a dataclass, but we only use .payload and
    # .from_instance attrs — a namespace is cheaper and clearer.
    return SimpleNamespace(
        event_type=event_type,
        payload=payload,
        from_instance=from_instance,
    )


@pytest.fixture
def handler():
    bus = _RecordingBus()
    space_repo = AsyncMock()
    space_repo.save_remote_invitation = AsyncMock()
    space_repo.get_invitation_by_token = AsyncMock()
    space_repo.update_invitation_status = AsyncMock()
    remote_members = AsyncMock()
    remote_members.add = AsyncMock()
    remote_members.remove = AsyncMock()
    h = PrivateSpaceInviteHandler(
        bus=bus,  # type: ignore[arg-type]
        space_repo=space_repo,
        remote_member_repo=remote_members,
    )
    return SimpleNamespace(
        h=h,
        bus=bus,
        space_repo=space_repo,
        remote_members=remote_members,
    )


async def test_invite_happy_path(handler):
    ev = _event(
        "SPACE_PRIVATE_INVITE",
        {
            "space_id": "sp1",
            "invite_token": "tkn",
            "invitee_user_id": "u1",
            "inviter_user_id": "u2",
            "space_display_hint": "Board",
        },
    )
    await handler.h._on_invite(ev)
    handler.space_repo.save_remote_invitation.assert_awaited_once()
    assert any(isinstance(e, RemoteSpaceInviteReceived) for e in handler.bus.events)


async def test_invite_missing_fields_noops(handler):
    ev = _event("SPACE_PRIVATE_INVITE", {})
    await handler.h._on_invite(ev)
    handler.space_repo.save_remote_invitation.assert_not_awaited()
    assert handler.bus.events == []


async def test_accept_no_token_noops(handler):
    ev = _event("SPACE_PRIVATE_INVITE_ACCEPT", {})
    await handler.h._on_accept(ev)
    handler.remote_members.add.assert_not_awaited()
    assert handler.bus.events == []


async def test_accept_unknown_token_noops(handler):
    handler.space_repo.get_invitation_by_token.return_value = None
    ev = _event("SPACE_PRIVATE_INVITE_ACCEPT", {"invite_token": "x"})
    await handler.h._on_accept(ev)
    handler.remote_members.add.assert_not_awaited()


async def test_accept_happy_path(handler):
    handler.space_repo.get_invitation_by_token.return_value = {
        "id": 42,
        "space_id": "sp-a",
    }
    ev = _event(
        "SPACE_PRIVATE_INVITE_ACCEPT",
        {
            "invite_token": "abc",
            "invitee_user_id": "u1",
            "invitee_public_key": "pk",
            "invitee_display_name": "Bob",
        },
    )
    await handler.h._on_accept(ev)
    handler.remote_members.add.assert_awaited_once()
    handler.space_repo.update_invitation_status.assert_awaited_with(42, "accepted")
    assert any(isinstance(e, RemoteSpaceInviteAccepted) for e in handler.bus.events)


async def test_decline_no_token_noops(handler):
    ev = _event("SPACE_PRIVATE_INVITE_DECLINE", {})
    await handler.h._on_decline(ev)
    handler.space_repo.update_invitation_status.assert_not_awaited()


async def test_decline_unknown_token_noops(handler):
    handler.space_repo.get_invitation_by_token.return_value = None
    ev = _event("SPACE_PRIVATE_INVITE_DECLINE", {"invite_token": "nope"})
    await handler.h._on_decline(ev)
    handler.space_repo.update_invitation_status.assert_not_awaited()


async def test_decline_happy_path(handler):
    handler.space_repo.get_invitation_by_token.return_value = {
        "id": 5,
        "space_id": "sp-b",
    }
    ev = _event(
        "SPACE_PRIVATE_INVITE_DECLINE",
        {"invite_token": "tk", "invitee_user_id": "u1"},
    )
    await handler.h._on_decline(ev)
    handler.space_repo.update_invitation_status.assert_awaited_with(5, "declined")
    assert any(isinstance(e, RemoteSpaceInviteDeclined) for e in handler.bus.events)


async def test_member_removed_missing_fields_noops(handler):
    ev = _event("SPACE_REMOTE_MEMBER_REMOVED", {})
    await handler.h._on_member_removed(ev)
    handler.remote_members.remove.assert_not_awaited()


async def test_member_removed_happy_path(handler):
    ev = _event(
        "SPACE_REMOTE_MEMBER_REMOVED",
        {"space_id": "sp-c", "user_id": "u-bye"},
    )
    await handler.h._on_member_removed(ev)
    handler.remote_members.remove.assert_awaited_once_with(
        "sp-c",
        "peer-1",
        "u-bye",
    )
    assert any(isinstance(e, RemoteSpaceMemberRemoved) for e in handler.bus.events)


async def test_attach_to_registers_four_handlers():
    """`attach_to` wires the four event-type → handler bindings."""
    bus = EventBus()
    space_repo = AsyncMock()
    remote_members = AsyncMock()
    h = PrivateSpaceInviteHandler(
        bus=bus,  # type: ignore[arg-type]
        space_repo=space_repo,
        remote_member_repo=remote_members,
    )

    class _FakeRegistry:
        def __init__(self) -> None:
            self.bindings: dict = {}

        def register(self, event_type, handler):
            self.bindings[event_type] = handler

    class _FakeFedSvc:
        def __init__(self) -> None:
            self._event_registry = _FakeRegistry()

    fed = _FakeFedSvc()
    h.attach_to(fed)  # type: ignore[arg-type]
    assert len(fed._event_registry.bindings) == 4
