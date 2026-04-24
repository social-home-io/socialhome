"""Release-blocker protocol tests for direct-peer space sync (§25.6).

Marked ``@pytest.mark.security`` — CLAUDE.md requires these to run
before every commit touching federation code.

Coverage:
* Forged outer signature — verification rejects and nothing persists.
* AAD-binding — a chunk with a ``sync_id`` that mismatches the one used
  in the AAD triggers a decrypt failure and nothing persists.
* Unknown resource string — dispatched to the "drop" path, no crash,
  no side effects.
* Tampered sentinel signature — no ``SpaceSyncComplete`` event is
  published.
"""

from __future__ import annotations

from types import SimpleNamespace

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

pytestmark = pytest.mark.security


# ─── Stubs ──────────────────────────────────────────────────────────────


class _AadCheckingCrypto:
    """Reject when (space_id, epoch, sync_id) AAD doesn't match what was
    used to encrypt. Mirrors the real AES-GCM behaviour in a pure-Python
    way without pulling in AES-GCM state.
    """

    def __init__(self) -> None:
        self.epoch = 0

    async def encrypt_chunk(self, *, space_id, sync_id, plaintext):
        import base64

        aad = f"{space_id}:{self.epoch}:{sync_id}".encode("utf-8")
        blob = aad + b"|" + plaintext
        return self.epoch, base64.urlsafe_b64encode(blob).decode("ascii")

    async def decrypt_chunk(self, *, space_id, epoch, sync_id, ciphertext):
        import base64

        blob = base64.urlsafe_b64decode(ciphertext)
        expected = f"{space_id}:{epoch}:{sync_id}".encode("utf-8")
        aad, _, body = blob.partition(b"|")
        if aad != expected:
            raise RuntimeError("AAD mismatch (tampered sync_id or epoch)")
        return body


class _FakeFedRepo:
    def __init__(self, peer):
        self._peer = peer

    async def get_instance(self, iid):
        return self._peer if iid == self._peer.id else None


class _Stub:
    def __init__(self):
        self.saved = []

    async def save(self, obj, *args):
        self.saved.append((obj, args) if args else obj)
        return obj

    async def save_member(self, member):
        self.saved.append(member)
        return member

    async def ban_member(
        self, *, space_id, user_id, banned_by, identity_pk=None, reason=None
    ):
        self.saved.append((space_id, user_id, banned_by, reason))

    async def add_comment(self, comment):
        self.saved.append(comment)
        return comment

    async def save_event(self, space_id, event):
        self.saved.append((space_id, event))
        return event

    async def create_album(self, album):
        self.saved.append(("album", album))

    async def create_item(self, item):
        self.saved.append(("item", item))


def _peer():
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
def env():
    bus = EventBus()
    peer, kp = _peer()
    self_kp = generate_identity_keypair()
    stub = _Stub()
    r = SpaceSyncReceiver(
        bus=bus,
        encoder=FederationEncoder(self_kp.private_key),
        crypto=_AadCheckingCrypto(),
        federation_repo=_FakeFedRepo(peer),
        space_repo=stub,
        space_post_repo=stub,
        space_task_repo=stub,
        page_repo=stub,
        sticky_repo=stub,
        space_calendar_repo=stub,
        gallery_repo=stub,
    )
    return SimpleNamespace(bus=bus, receiver=r, peer_kp=kp, stub=stub)


def _sign(envelope: dict, kp) -> dict:
    enc = FederationEncoder(kp.private_key)
    signed = {k: v for k, v in envelope.items() if k != "signatures"}
    envelope["signatures"] = enc.sign_envelope_all(
        orjson.dumps(signed),
        suite="ed25519",
    )
    return envelope


async def _encrypt(crypto, *, space_id, sync_id, records):
    _, ct = await crypto.encrypt_chunk(
        space_id=space_id,
        sync_id=sync_id,
        plaintext=orjson.dumps({"records": records}),
    )
    return ct


# ─── Tests ─────────────────────────────────────────────────────────────


async def test_forged_signature_is_rejected(env):
    """Envelope signed by a different identity keypair must NOT persist."""
    imposter = generate_identity_keypair()
    ct = await _encrypt(
        _AadCheckingCrypto(),
        space_id="sp-1",
        sync_id="sync-1",
        records=[
            {
                "user_id": "u-bad",
                "role": "member",
                "joined_at": "2026-04-18T00:00:00+00:00",
            }
        ],
    )
    envelope = {
        "sync_id": "sync-1",
        "resource": "members",
        "space_id": "sp-1",
        "epoch": 0,
        "seq_start": 0,
        "seq_end": 1,
        "is_last": False,
        "encrypted_payload": ct,
    }
    _sign(envelope, imposter)  # wrong signer
    await env.receiver.on_chunk(
        serialise_chunk(envelope),
        from_instance="peer-a",
    )
    assert env.stub.saved == []


async def test_mismatched_sync_id_triggers_aad_decrypt_failure(env):
    """Encrypt under sync_id=X, ship envelope claiming sync_id=Y — the
    AAD no longer matches and decrypt fails. Nothing persists.
    """
    ct = await _encrypt(
        _AadCheckingCrypto(),
        space_id="sp-1",
        sync_id="sync-ORIGINAL",
        records=[
            {
                "user_id": "u-1",
                "role": "member",
                "joined_at": "2026-04-18T00:00:00+00:00",
            }
        ],
    )
    envelope = {
        "sync_id": "sync-REPLAY",  # tampered
        "resource": "members",
        "space_id": "sp-1",
        "epoch": 0,
        "seq_start": 0,
        "seq_end": 1,
        "is_last": False,
        "encrypted_payload": ct,
    }
    _sign(envelope, env.peer_kp)
    await env.receiver.on_chunk(
        serialise_chunk(envelope),
        from_instance="peer-a",
    )
    assert env.stub.saved == []


async def test_unknown_resource_drops(env):
    """An envelope with a ``resource`` string we don't know must be
    dispatched to the drop path — no crash, no partial writes.
    """
    ct = await _encrypt(
        _AadCheckingCrypto(),
        space_id="sp-1",
        sync_id="sync-1",
        records=[{"whatever": "value"}],
    )
    envelope = {
        "sync_id": "sync-1",
        "resource": "not_a_resource",
        "space_id": "sp-1",
        "epoch": 0,
        "seq_start": 0,
        "seq_end": 1,
        "is_last": False,
        "encrypted_payload": ct,
    }
    _sign(envelope, env.peer_kp)
    await env.receiver.on_chunk(
        serialise_chunk(envelope),
        from_instance="peer-a",
    )
    assert env.stub.saved == []


async def test_tampered_sentinel_does_not_fire_completion(env):
    """The terminal sentinel is signed-but-not-encrypted. If a peer
    corrupts the signature, the receiver must NOT publish
    :class:`SpaceSyncComplete`.
    """
    captured: list[SpaceSyncComplete] = []

    async def _on_complete(event: SpaceSyncComplete) -> None:
        captured.append(event)

    env.bus.subscribe(SpaceSyncComplete, _on_complete)
    sentinel = {
        "sync_id": "sync-1",
        "resource": SENTINEL_RESOURCE,
        "space_id": "sp-1",
        "is_last": True,
        "signatures": {"ed25519": "0" * 128},  # bogus
    }
    await env.receiver.on_chunk(
        serialise_chunk(sentinel),
        from_instance="peer-a",
    )
    assert captured == []
