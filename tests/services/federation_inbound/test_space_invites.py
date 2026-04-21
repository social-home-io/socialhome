"""Tests for :class:`SpaceInviteInboundHandlers` (§11.2)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from social_home.domain.events import (
    RemoteSpaceInviteReceived,
    RemoteSpaceJoinRequestReceived,
)
from social_home.domain.federation import FederationEvent, FederationEventType
from social_home.infrastructure.event_bus import EventBus
from social_home.services.federation_inbound import SpaceInviteInboundHandlers


class _FakeRegistry:
    def __init__(self) -> None:
        self.registered = []

    def register(self, t, h):
        self.registered.append((t, h))


class _FakeFederationService:
    def __init__(self) -> None:
        self._event_registry = _FakeRegistry()


class _FakeSpaceRepo:
    def __init__(self) -> None:
        self.invitations = []
        self.join_requests = []
        self.join_status = []

    async def save_invitation(
        self, *, space_id, invited_user_id, invited_by, ttl_days=7
    ):
        self.invitations.append((space_id, invited_user_id, invited_by))
        return "inv-1"

    async def save_join_request(
        self,
        space_id=None,
        user_id=None,
        *,
        message=None,
        ttl_days=7,
        remote_applicant_instance_id=None,
        remote_applicant_pk=None,
        request_id=None,
    ):
        # Accept both positional (legacy) and keyword calls.
        self.join_requests.append(
            (
                space_id,
                user_id,
                message,
                remote_applicant_instance_id,
                request_id,
            )
        )
        return request_id or "req-1"

    async def update_join_request_status(self, request_id, status, *, reviewed_by=None):
        self.join_status.append((request_id, status, reviewed_by))


def _event(event_type, payload, *, from_instance="peer-a", space_id=None):
    return FederationEvent(
        msg_id="m",
        event_type=event_type,
        from_instance=from_instance,
        to_instance="self",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
        space_id=space_id,
    )


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def repo():
    return _FakeSpaceRepo()


@pytest.fixture
def handlers(bus, repo):
    h = SpaceInviteInboundHandlers(bus=bus, space_repo=repo)
    h.attach_to(_FakeFederationService())
    return h


async def test_attach_registers_expected_event_types(bus, repo):
    h = SpaceInviteInboundHandlers(bus=bus, space_repo=repo)
    fed = _FakeFederationService()
    h.attach_to(fed)
    types = {t for t, _ in fed._event_registry.registered}
    assert FederationEventType.SPACE_INVITE in types
    assert FederationEventType.SPACE_INVITE_VIA in types
    assert FederationEventType.SPACE_JOIN_REQUEST in types
    assert FederationEventType.SPACE_JOIN_REQUEST_APPROVED in types
    assert FederationEventType.SPACE_JOIN_REQUEST_DENIED in types
    assert FederationEventType.SPACE_JOIN_REQUEST_EXPIRED in types
    assert FederationEventType.SPACE_JOIN_REQUEST_WITHDRAWN in types


async def test_invite_persists_and_publishes(bus, repo, handlers):
    captured: list[RemoteSpaceInviteReceived] = []
    bus.subscribe(RemoteSpaceInviteReceived, captured.append)
    await handlers._on_invite(
        _event(
            FederationEventType.SPACE_INVITE,
            {"invitee_user_id": "u-1", "inviter_user_id": "u-admin"},
            space_id="sp-1",
        )
    )
    assert repo.invitations == [("sp-1", "u-1", "u-admin")]
    assert captured[0].invitee_user_id == "u-1"


async def test_invite_missing_fields_drops(repo, handlers):
    await handlers._on_invite(
        _event(
            FederationEventType.SPACE_INVITE,
            {},
            space_id="sp-1",
        )
    )
    assert repo.invitations == []


async def test_invite_via_delegates_to_invite(bus, repo, handlers):
    captured: list[RemoteSpaceInviteReceived] = []
    bus.subscribe(RemoteSpaceInviteReceived, captured.append)
    await handlers._on_invite_via(
        _event(
            FederationEventType.SPACE_INVITE_VIA,
            {"invitee_user_id": "u-1", "inviter_user_id": "u-admin"},
            space_id="sp-1",
        )
    )
    assert len(repo.invitations) == 1


async def test_join_request_persists_and_publishes(bus, repo, handlers):
    captured: list[RemoteSpaceJoinRequestReceived] = []

    async def _append(e):
        captured.append(e)

    bus.subscribe(RemoteSpaceJoinRequestReceived, _append)
    await handlers._on_join_request(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST,
            {"user_id": "u-1", "message": "please let me in"},
            space_id="sp-1",
        )
    )
    assert repo.join_requests == [
        ("sp-1", "u-1", "please let me in", "peer-a", None),
    ]
    assert captured[0].requester_user_id == "u-1"


async def test_join_request_missing_user_id_drops(repo, handlers):
    await handlers._on_join_request(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST,
            {},
            space_id="sp-1",
        )
    )
    assert repo.join_requests == []


async def test_join_request_status_approved(repo, handlers):
    await handlers._on_join_request_status(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST_APPROVED,
            {"request_id": "req-1", "reviewed_by": "admin-a"},
        )
    )
    assert repo.join_status == [("req-1", "approved", "admin-a")]


async def test_join_request_status_denied(repo, handlers):
    await handlers._on_join_request_status(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST_DENIED,
            {"request_id": "req-1"},
        )
    )
    assert repo.join_status == [("req-1", "denied", None)]


async def test_join_request_status_withdrawn(repo, handlers):
    await handlers._on_join_request_status(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST_WITHDRAWN,
            {"request_id": "req-1"},
        )
    )
    assert repo.join_status == [("req-1", "withdrawn", None)]


async def test_join_request_status_expired(repo, handlers):
    await handlers._on_join_request_status(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST_EXPIRED,
            {"request_id": "req-1"},
        )
    )
    assert repo.join_status == [("req-1", "expired", None)]


async def test_join_request_reply_via_uses_status_field(repo, handlers):
    await handlers._on_join_request_status(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST_REPLY_VIA,
            {"request_id": "req-1", "status": "approved"},
        )
    )
    assert repo.join_status == [("req-1", "approved", None)]


async def test_join_request_status_missing_id_is_noop(repo, handlers):
    await handlers._on_join_request_status(
        _event(
            FederationEventType.SPACE_JOIN_REQUEST_APPROVED,
            {},
        )
    )
    assert repo.join_status == []
