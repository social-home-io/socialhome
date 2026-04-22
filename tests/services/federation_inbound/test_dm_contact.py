"""Tests for the ``DM_CONTACT_REQUEST`` handler on
:class:`PairingInboundHandlers` (§23.47)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.domain.events import DmContactRequested
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.federation_inbound import PairingInboundHandlers


class _FakeRegistry:
    def __init__(self) -> None:
        self.registered = []

    def register(self, t, h):
        self.registered.append((t, h))


class _FakeFederationService:
    def __init__(self) -> None:
        self._event_registry = _FakeRegistry()


class _FakeFederationRepo:
    async def get_instance(self, iid):
        return None

    async def get_pairing(self, token):
        return None

    async def save_instance(self, inst):
        return inst

    async def delete_instance(self, iid):
        pass

    async def delete_pairing(self, token):
        pass


class _FakeDmContactRepo:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str]] = []
        self.raise_on_save = False

    async def save_request(self, *, from_user_id, to_user_id):
        if self.raise_on_save:
            raise ValueError("FK violation")
        self.saved.append((from_user_id, to_user_id))
        return "req-1"

    async def list_pending_for(self, to_user_id):
        return []

    async def set_status(self, request_id, status):
        pass


def _event(payload, from_instance="peer-a"):
    return FederationEvent(
        msg_id="m",
        event_type=FederationEventType.DM_CONTACT_REQUEST,
        from_instance=from_instance,
        to_instance="self",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
    )


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def repo():
    return _FakeDmContactRepo()


@pytest.fixture
def handlers(bus, repo):
    h = PairingInboundHandlers(
        bus=bus,
        federation_repo=_FakeFederationRepo(),
        dm_contact_repo=repo,
    )
    fed = _FakeFederationService()
    h.attach_to(fed)
    return h, fed


def test_attach_registers_dm_contact_request_only_when_repo_given(bus):
    """Without a dm_contact_repo the handler doesn't self-register —
    keeps the federation registry clean for deployments that don't
    want the contact-request flow."""
    h_without = PairingInboundHandlers(
        bus=bus,
        federation_repo=_FakeFederationRepo(),
    )
    fed = _FakeFederationService()
    h_without.attach_to(fed)
    types = {t for t, _ in fed._event_registry.registered}
    assert FederationEventType.DM_CONTACT_REQUEST not in types


def test_attach_registers_dm_contact_request_when_repo_given(handlers):
    _, fed = handlers
    types = {t for t, _ in fed._event_registry.registered}
    assert FederationEventType.DM_CONTACT_REQUEST in types


async def test_contact_request_persists_and_publishes(bus, repo, handlers):
    h, _ = handlers
    captured: list[DmContactRequested] = []
    bus.subscribe(DmContactRequested, captured.append)

    await h._on_contact_request(
        _event(
            {
                "requester_user_id": "u-remote",
                "requester_display_name": "Alice",
                "recipient_user_id": "u-local",
            }
        )
    )

    assert repo.saved == [("u-remote", "u-local")]
    assert len(captured) == 1
    assert captured[0].requester_user_id == "u-remote"
    assert captured[0].recipient_user_id == "u-local"
    assert captured[0].requester_display_name == "Alice"


async def test_contact_request_accepts_legacy_field_names(bus, repo, handlers):
    """``from_user_id`` / ``to_user_id`` aliases work too — some older
    peers may send those instead of the current spec names."""
    h, _ = handlers
    await h._on_contact_request(
        _event(
            {
                "from_user_id": "u-1",
                "to_user_id": "u-2",
            }
        )
    )
    assert repo.saved == [("u-1", "u-2")]


async def test_contact_request_missing_ids_drops(bus, repo, handlers):
    h, _ = handlers
    captured: list[DmContactRequested] = []
    bus.subscribe(DmContactRequested, captured.append)

    await h._on_contact_request(_event({}))
    assert repo.saved == []
    assert captured == []


async def test_contact_request_fk_failure_drops_silently(bus, repo, handlers):
    """If the local recipient row doesn't exist yet, the FK fails —
    handler logs and returns instead of blowing up the pipeline."""
    h, _ = handlers
    repo.raise_on_save = True
    captured: list[DmContactRequested] = []
    bus.subscribe(DmContactRequested, captured.append)

    await h._on_contact_request(
        _event(
            {
                "requester_user_id": "u-remote",
                "recipient_user_id": "u-missing",
            }
        )
    )
    # Repo raised → nothing saved, event not published (it would be
    # pointing at a row that doesn't exist).
    assert repo.saved == []
    assert captured == []
