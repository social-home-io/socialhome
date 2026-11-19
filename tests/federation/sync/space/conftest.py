"""Fixtures shared by the space-sync receiver tests (``test_receiver.py``
and the per-resource ``test_receive_*.py``); the fakes they build are in
``receiver_fakes.py``."""

from __future__ import annotations

import pytest

from socialhome.crypto import generate_identity_keypair
from socialhome.domain.federation import InstanceSource, PairingStatus, RemoteInstance
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.sync.space.receiver import SpaceSyncReceiver
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services import calendar_service, page_conflict_service
from tests.federation.sync.space.receiver_fakes import (
    _BazaarRepoStub,
    _CalendarRepoStub,
    _FakeCrypto,
    _FakeFedRepo,
    _FakeRepos,
    _FakeSpacePostRepo,
    _FakeSpaceRepo,
    _GalleryRepoStub,
    _make_peer,
    _PageRepoStub,
    _PostRepoStub,
    _SpaceRepoStub,
    _StickyRepoStub,
    _Stub,
    _TaskRepoStub,
    _ZoneRepoStub,
)


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def peer_setup():
    return _make_peer()


@pytest.fixture
def receiver(bus, peer_setup):
    peer, _ = peer_setup
    # Encoder with matching private key for the provider signing side.
    # Receiver only verifies — use a dummy seed for its encoder because
    # we only test verify paths here.
    kp_self = generate_identity_keypair()
    encoder = FederationEncoder(kp_self.private_key)
    space_repo = _FakeSpaceRepo()
    space_post_repo = _FakeSpacePostRepo()
    task_repo = _Stub()
    page_repo = _Stub()
    sticky_repo = _Stub()
    cal_repo = _Stub()
    gallery_repo = _Stub()
    r = SpaceSyncReceiver(
        bus=bus,
        encoder=encoder,
        crypto=_FakeCrypto(),
        federation_repo=_FakeFedRepo(peer),
        space_repo=space_repo,
        space_post_repo=space_post_repo,
        space_task_repo=task_repo,
        page_repo=page_repo,
        sticky_repo=sticky_repo,
        space_calendar_repo=cal_repo,
        gallery_repo=gallery_repo,
    )
    return r, space_repo, space_post_repo


@pytest.fixture
def peer():
    kp = generate_identity_keypair()
    return (
        RemoteInstance(
            id="peer-a",
            display_name="Peer A",
            remote_identity_pk=kp.public_key.hex(),
            key_self_to_remote="enc",
            key_remote_to_self="enc",
            remote_inbox_url="https://peer/wh",
            local_inbox_id="wh-peer-a",
            status=PairingStatus.CONFIRMED,
            source=InstanceSource.MANUAL,
        ),
        kp,
    )


@pytest.fixture
def setup(bus, peer):
    peer_inst, peer_kp = peer
    collector = _FakeRepos()
    self_kp = generate_identity_keypair()
    r = SpaceSyncReceiver(
        bus=bus,
        encoder=FederationEncoder(self_kp.private_key),
        crypto=_FakeCrypto(),
        federation_repo=_FakeFedRepo(peer_inst),
        space_repo=_SpaceRepoStub(collector),
        space_post_repo=_PostRepoStub(collector),
        space_task_repo=_TaskRepoStub(collector),
        page_repo=_PageRepoStub(collector),
        sticky_repo=_StickyRepoStub(collector),
        space_calendar_repo=_CalendarRepoStub(collector),
        gallery_repo=_GalleryRepoStub(collector),
        zone_repo=_ZoneRepoStub(collector),
        bazaar_repo=_BazaarRepoStub(collector),
    )
    return r, collector, peer_kp


@pytest.fixture
def small_limits(monkeypatch):
    """The page and calendar limits (whole records), small enough to test."""
    monkeypatch.setattr(calendar_service, "SPACE_CALENDAR_ITEM_MAX_BYTES", 2000)
    monkeypatch.setattr(page_conflict_service, "SPACE_PAGE_ITEM_MAX_BYTES", 2000)
