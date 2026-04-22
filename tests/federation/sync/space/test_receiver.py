"""Unit tests for :class:`SpaceSyncReceiver`."""

from __future__ import annotations


import orjson
import pytest

from socialhome.crypto import generate_identity_keypair
from socialhome.domain.events import SpaceSyncComplete
from socialhome.domain.federation import (
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.sync.space.exporter import (
    SENTINEL_RESOURCE,
    serialise_chunk,
)
from socialhome.federation.sync.space.receiver import SpaceSyncReceiver
from socialhome.infrastructure.event_bus import EventBus


class _FakeCrypto:
    def __init__(self) -> None:
        self.epoch = 0

    async def encrypt_chunk(self, *, space_id, sync_id, plaintext):
        import base64

        return self.epoch, base64.urlsafe_b64encode(plaintext).decode("ascii")

    async def decrypt_chunk(self, *, space_id, epoch, sync_id, ciphertext):
        import base64

        return base64.urlsafe_b64decode(ciphertext)


class _FakeFedRepo:
    def __init__(self, peer):
        self._peer = peer

    async def get_instance(self, iid):
        return self._peer if (self._peer and self._peer.id == iid) else None


class _FakeSpaceRepo:
    def __init__(self):
        self.members = []
        self.bans = []

    async def save_member(self, member):
        self.members.append(member)
        return member

    async def ban_member(
        self, *, space_id, user_id, banned_by, identity_pk=None, reason=None
    ):
        self.bans.append((space_id, user_id, banned_by, reason))


class _FakeSpacePostRepo:
    def __init__(self):
        self.saved = []
        self.comments = []

    async def save(self, space_id, post):
        self.saved.append((space_id, post))
        return post

    async def add_comment(self, comment):
        self.comments.append(comment)
        return comment


class _Stub:
    """Generic stub with a list of saved items."""

    def __init__(self):
        self.saved = []

    async def save(self, obj, *args):
        self.saved.append((obj, args) if args else obj)
        return obj

    async def save_event(self, space_id, event):
        self.saved.append((space_id, event))
        return event

    async def create_album(self, album):
        self.saved.append(("album", album))

    async def create_item(self, item):
        self.saved.append(("item", item))


def _make_peer() -> tuple[RemoteInstance, object]:
    kp = generate_identity_keypair()
    peer = RemoteInstance(
        id="peer-a",
        display_name="Peer A",
        remote_identity_pk=kp.public_key.hex(),
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_webhook_url="https://peer/wh",
        local_webhook_id="wh-peer-a",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    return peer, kp


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


async def _sign_as_peer(kp, envelope):
    """Sign an envelope as if we were the peer."""
    encoder = FederationEncoder(kp.private_key)
    bytes_to_sign = orjson.dumps(
        {k: v for k, v in envelope.items() if k != "signatures"}
    )
    envelope["signatures"] = encoder.sign_envelope_all(
        bytes_to_sign,
        suite="ed25519",
    )
    return envelope


async def test_on_chunk_persists_members(receiver, peer_setup):
    r, space_repo, _ = receiver
    peer, kp = peer_setup
    crypto = _FakeCrypto()
    # Build an encrypted payload for "members" resource.
    plaintext = orjson.dumps(
        {
            "records": [
                {
                    "user_id": "u-1",
                    "role": "member",
                    "joined_at": "2026-04-18T00:00:00+00:00",
                }
            ],
        }
    )
    _, ciphertext = await crypto.encrypt_chunk(
        space_id="sp-1",
        sync_id="sync-1",
        plaintext=plaintext,
    )
    envelope = {
        "sync_id": "sync-1",
        "resource": "members",
        "space_id": "sp-1",
        "epoch": 0,
        "seq_start": 0,
        "seq_end": 1,
        "is_last": False,
        "encrypted_payload": ciphertext,
    }
    envelope = await _sign_as_peer(kp, envelope)
    await r.on_chunk(
        serialise_chunk(envelope),
        from_instance="peer-a",
    )
    assert len(space_repo.members) == 1
    assert space_repo.members[0].user_id == "u-1"


async def test_on_chunk_sentinel_publishes_completion(bus, receiver, peer_setup):
    r, _, _ = receiver
    peer, kp = peer_setup
    captured: list[SpaceSyncComplete] = []
    bus.subscribe(SpaceSyncComplete, captured.append)
    sentinel = {
        "sync_id": "sync-1",
        "resource": SENTINEL_RESOURCE,
        "space_id": "sp-1",
        "is_last": True,
    }
    sentinel = await _sign_as_peer(kp, sentinel)
    await r.on_chunk(serialise_chunk(sentinel), from_instance="peer-a")
    assert len(captured) == 1
    assert captured[0].space_id == "sp-1"
    assert captured[0].from_instance == "peer-a"


async def test_on_chunk_rejects_unknown_peer(receiver, peer_setup):
    r, space_repo, _ = receiver
    peer, kp = peer_setup
    envelope = {
        "sync_id": "sync-1",
        "resource": "members",
        "space_id": "sp-1",
        "epoch": 0,
        "seq_start": 0,
        "seq_end": 0,
        "is_last": False,
        "encrypted_payload": "x:y",
    }
    envelope = await _sign_as_peer(kp, envelope)
    await r.on_chunk(
        serialise_chunk(envelope),
        from_instance="peer-unknown",
    )
    assert space_repo.members == []


async def test_on_chunk_rejects_tampered_signature(receiver, peer_setup):
    r, space_repo, _ = receiver
    peer, kp = peer_setup
    envelope = {
        "sync_id": "sync-1",
        "resource": "members",
        "space_id": "sp-1",
        "epoch": 0,
        "seq_start": 0,
        "seq_end": 0,
        "is_last": False,
        "encrypted_payload": "x:y",
        "signatures": {"ed25519": "definitely-not-a-real-sig"},
    }
    await r.on_chunk(
        serialise_chunk(envelope),
        from_instance="peer-a",
    )
    assert space_repo.members == []


async def test_on_chunk_rejects_malformed_json(receiver):
    r, space_repo, _ = receiver
    await r.on_chunk(b"{not json}", from_instance="peer-a")
    assert space_repo.members == []


async def test_on_chunk_unknown_resource_drops(receiver, peer_setup):
    r, space_repo, _ = receiver
    peer, kp = peer_setup
    envelope = {
        "sync_id": "sync-1",
        "resource": "banana",
        "space_id": "sp-1",
        "epoch": 0,
        "seq_start": 0,
        "seq_end": 0,
        "is_last": False,
        "encrypted_payload": "x:y",
    }
    envelope = await _sign_as_peer(kp, envelope)
    # Should log + return, not raise.
    await r.on_chunk(serialise_chunk(envelope), from_instance="peer-a")
