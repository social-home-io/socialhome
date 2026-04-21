"""Tests for :class:`SpaceMembershipInboundHandlers` (§13)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from social_home.domain.events import (
    RemoteSpaceCreated,
    RemoteSpaceDissolved,
    RemoteSpaceMemberBanned,
)
from social_home.domain.federation import FederationEvent, FederationEventType
from social_home.domain.space import JoinMode, SpaceType
from social_home.infrastructure.event_bus import EventBus
from social_home.services.federation_inbound import SpaceMembershipInboundHandlers


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
        self.saved = []
        self.dissolved = []
        self.instance_removes = []
        self.bans = []
        self.unbans = []
        self.age_gates = []
        self.spaces: dict = {}

    async def save(self, space):
        self.saved.append(space)
        self.spaces[space.id] = space
        return space

    async def mark_dissolved(self, space_id):
        self.dissolved.append(space_id)

    async def remove_space_instance(self, space_id, instance_id):
        self.instance_removes.append((space_id, instance_id))

    async def ban_member(
        self, *, space_id, user_id, banned_by, identity_pk=None, reason=None
    ):
        self.bans.append((space_id, user_id, banned_by, reason))

    async def unban_member(self, space_id, user_id):
        self.unbans.append((space_id, user_id))

    async def update_age_gate(self, space_id, *, min_age=None, target_audience=None):
        self.age_gates.append((space_id, min_age, target_audience))

    async def get(self, space_id):
        return self.spaces.get(space_id)


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
    h = SpaceMembershipInboundHandlers(bus=bus, space_repo=repo)
    h.attach_to(_FakeFederationService())
    return h


async def test_attach_registers_six_event_types(bus, repo):
    h = SpaceMembershipInboundHandlers(bus=bus, space_repo=repo)
    fed = _FakeFederationService()
    h.attach_to(fed)
    types = {t for t, _ in fed._event_registry.registered}
    assert types == {
        FederationEventType.SPACE_CREATED,
        FederationEventType.SPACE_DISSOLVED,
        FederationEventType.SPACE_INSTANCE_LEFT,
        FederationEventType.SPACE_MEMBER_BANNED,
        FederationEventType.SPACE_MEMBER_UNBANNED,
        FederationEventType.SPACE_AGE_GATE_UPDATED,
        FederationEventType.SPACE_CONFIG_CATCH_UP,
    }


async def test_space_created_persists_and_publishes(bus, repo, handlers):
    captured: list[RemoteSpaceCreated] = []
    bus.subscribe(RemoteSpaceCreated, captured.append)
    await handlers._on_created(
        _event(
            FederationEventType.SPACE_CREATED,
            {
                "name": "Space 1",
                "owner_username": "owner",
                "identity_public_key": "aa" * 32,
                "space_type": SpaceType.HOUSEHOLD.value,
                "join_mode": JoinMode.INVITE_ONLY.value,
                "config_sequence": 5,
            },
            space_id="sp-1",
        )
    )
    assert len(repo.saved) == 1
    assert repo.saved[0].id == "sp-1"
    assert repo.saved[0].config_sequence == 5
    assert captured[0].space_id == "sp-1"


async def test_space_created_missing_identity_key_drops(repo, handlers):
    await handlers._on_created(
        _event(
            FederationEventType.SPACE_CREATED,
            {"name": "X"},
            space_id="sp-1",
        )
    )
    assert repo.saved == []


async def test_space_dissolved_marks_and_publishes(bus, repo, handlers):
    captured: list[RemoteSpaceDissolved] = []
    bus.subscribe(RemoteSpaceDissolved, captured.append)
    await handlers._on_dissolved(
        _event(
            FederationEventType.SPACE_DISSOLVED,
            {},
            space_id="sp-1",
        )
    )
    assert repo.dissolved == ["sp-1"]
    assert captured[0].space_id == "sp-1"


async def test_instance_left_removes_row(repo, handlers):
    await handlers._on_instance_left(
        _event(
            FederationEventType.SPACE_INSTANCE_LEFT,
            {},
            space_id="sp-1",
            from_instance="peer-a",
        )
    )
    assert repo.instance_removes == [("sp-1", "peer-a")]


async def test_member_banned_persists_and_publishes(bus, repo, handlers):
    captured: list[RemoteSpaceMemberBanned] = []
    bus.subscribe(RemoteSpaceMemberBanned, captured.append)
    await handlers._on_banned(
        _event(
            FederationEventType.SPACE_MEMBER_BANNED,
            {"user_id": "u-1", "banned_by": "admin-a", "reason": "spam"},
            space_id="sp-1",
        )
    )
    assert repo.bans == [("sp-1", "u-1", "admin-a", "spam")]
    assert captured[0].user_id == "u-1"


async def test_member_banned_falls_back_to_from_instance_when_no_banned_by(
    repo,
    handlers,
):
    await handlers._on_banned(
        _event(
            FederationEventType.SPACE_MEMBER_BANNED,
            {"user_id": "u-1"},
            space_id="sp-1",
        )
    )
    assert repo.bans == [("sp-1", "u-1", "peer-a", None)]


async def test_member_unbanned_removes_ban(repo, handlers):
    await handlers._on_unbanned(
        _event(
            FederationEventType.SPACE_MEMBER_UNBANNED,
            {"user_id": "u-1"},
            space_id="sp-1",
        )
    )
    assert repo.unbans == [("sp-1", "u-1")]


async def test_age_gate_updates_min_age_only(repo, handlers):
    await handlers._on_age_gate(
        _event(
            FederationEventType.SPACE_AGE_GATE_UPDATED,
            {"min_age": 13},
            space_id="sp-1",
        )
    )
    assert repo.age_gates == [("sp-1", 13, None)]


async def test_age_gate_updates_target_audience_only(repo, handlers):
    await handlers._on_age_gate(
        _event(
            FederationEventType.SPACE_AGE_GATE_UPDATED,
            {"target_audience": "family"},
            space_id="sp-1",
        )
    )
    assert repo.age_gates == [("sp-1", None, "family")]


async def test_age_gate_empty_payload_is_noop(repo, handlers):
    await handlers._on_age_gate(
        _event(
            FederationEventType.SPACE_AGE_GATE_UPDATED,
            {},
            space_id="sp-1",
        )
    )
    assert repo.age_gates == []


async def test_config_catch_up_logs_when_behind(repo, handlers, caplog):
    """When remote sequence > local, log that we're behind."""
    from social_home.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceType,
    )
    import logging

    repo.spaces["sp-1"] = Space(
        id="sp-1",
        name="S",
        owner_instance_id="peer-a",
        owner_username="owner",
        identity_public_key="aa" * 32,
        config_sequence=2,
        features=SpaceFeatures(),
        space_type=SpaceType.HOUSEHOLD,
        join_mode=JoinMode.INVITE_ONLY,
    )
    with caplog.at_level(
        logging.INFO, logger="social_home.services.federation_inbound.space_membership"
    ):
        await handlers._on_catch_up(
            _event(
                FederationEventType.SPACE_CONFIG_CATCH_UP,
                {"sequence": 5},
                space_id="sp-1",
            )
        )
    assert any("we are behind" in rec.message for rec in caplog.records)


async def test_config_catch_up_unknown_space_is_noop(handlers):
    """No local row for the space → just drop."""
    # Should not raise.
    await handlers._on_catch_up(
        _event(
            FederationEventType.SPACE_CONFIG_CATCH_UP,
            {"sequence": 5},
            space_id="never-heard-of-it",
        )
    )


async def test_config_catch_up_missing_space_id_is_noop(handlers):
    await handlers._on_catch_up(
        _event(
            FederationEventType.SPACE_CONFIG_CATCH_UP,
            {"sequence": 5},
        )
    )
