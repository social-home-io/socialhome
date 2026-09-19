"""Unit tests for :class:`SpaceSyncReceiver`."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock
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
        #: space_id → host household identity pubkey (hex). Populated on a
        #: stub whose host we are NOT paired with; the receiver falls back
        #: to it to verify mesh-routed chunk signatures (#648).
        self.host_identity_pks: dict[str, str] = {}
        #: space_id → Space (or a stand-in exposing owner_instance_id).
        self.spaces: dict[str, object] = {}

    async def get_host_identity_pk(self, space_id):
        return self.host_identity_pks.get(space_id)

    async def set_host_identity_pk(self, space_id, pk_hex):
        self.host_identity_pks[space_id] = pk_hex

    async def get(self, space_id):
        return self.spaces.get(space_id)

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
        remote_inbox_url="https://peer/wh",
        local_inbox_id="wh-peer-a",
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
    """No paired-peer row AND no stored host key → drop.

    The #648 fallback must not turn this into an accept: an unpaired
    sender we hold no verified key for stays unauthenticated.
    """
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


# ─── Pending-decrypts cache (#122, PR #433) ────────────────────────────


class _MissingKeyCrypto:
    """Crypto stub that raises 'missing epoch' on first decrypt, then
    succeeds on the second call — simulates a key import in-between."""

    def __init__(self) -> None:
        self.attempts = 0

    async def encrypt_chunk(self, *, space_id, sync_id, plaintext):
        import base64

        return 5, base64.urlsafe_b64encode(plaintext).decode("ascii")

    async def decrypt_chunk(self, *, space_id, epoch, sync_id, ciphertext):
        import base64

        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError(
                f"SpaceContentEncryption: missing epoch {epoch} for space {space_id!r}",
            )
        return base64.urlsafe_b64decode(ciphertext)


async def test_decrypt_missing_epoch_stashes_in_cache_and_replays(
    bus,
    peer_setup,
):
    """The first chunk arrives before the key — decrypt raises with
    'missing epoch', the receiver stashes the redeliver in the cache,
    and the matching SpaceContentKeyImported event triggers a replay
    that succeeds. End state: row was persisted on the second try."""
    from socialhome.domain.events import SpaceContentKeyImported
    from socialhome.services.pending_decrypts_cache import PendingDecryptsCache

    peer, kp = peer_setup
    kp_self = generate_identity_keypair()
    encoder = FederationEncoder(kp_self.private_key)
    crypto = _MissingKeyCrypto()
    cache = PendingDecryptsCache(bus=bus)
    space_repo = _FakeSpaceRepo()
    r = SpaceSyncReceiver(
        bus=bus,
        encoder=encoder,
        crypto=crypto,
        federation_repo=_FakeFedRepo(peer),
        space_repo=space_repo,
        space_post_repo=_FakeSpacePostRepo(),
        space_task_repo=_Stub(),
        page_repo=_Stub(),
        sticky_repo=_Stub(),
        space_calendar_repo=_Stub(),
        gallery_repo=_Stub(),
        pending_decrypts=cache,
    )
    plaintext = orjson.dumps(
        {
            "records": [
                {
                    "user_id": "u-late",
                    "role": "member",
                    "joined_at": "2026-05-23T00:00:00+00:00",
                }
            ],
        }
    )
    import base64

    ciphertext = base64.urlsafe_b64encode(plaintext).decode("ascii")
    envelope = {
        "sync_id": "sync-late",
        "resource": "members",
        "space_id": "sp-late",
        "epoch": 5,
        "seq_start": 0,
        "seq_end": 1,
        "is_last": False,
        "encrypted_payload": ciphertext,
    }
    envelope = await _sign_as_peer(kp, envelope)

    # First arrival — key missing, gets stashed.
    await r.on_chunk(serialise_chunk(envelope), from_instance="peer-a")
    assert space_repo.members == []
    assert len(cache) == 1

    # Key arrives — cache drains, second on_chunk succeeds.
    await bus.publish(SpaceContentKeyImported(space_id="sp-late", epoch=5))
    assert len(cache) == 0
    assert len(space_repo.members) == 1
    assert space_repo.members[0].user_id == "u-late"
    assert crypto.attempts == 2


async def test_decrypt_failure_other_than_missing_key_still_drops(
    bus,
    peer_setup,
):
    """A tampered ciphertext (decrypt raises but NOT with 'missing
    epoch' in the message) MUST NOT stash — those failures are not
    race-recoverable and stashing them would be a leak."""
    from socialhome.services.pending_decrypts_cache import PendingDecryptsCache

    class _BadCrypto:
        async def encrypt_chunk(self, *, space_id, sync_id, plaintext):
            return 0, "x:y"

        async def decrypt_chunk(self, **kw):
            raise RuntimeError("InvalidTag: tampered ciphertext")

    peer, kp = peer_setup
    kp_self = generate_identity_keypair()
    cache = PendingDecryptsCache(bus=bus)
    r = SpaceSyncReceiver(
        bus=bus,
        encoder=FederationEncoder(kp_self.private_key),
        crypto=_BadCrypto(),
        federation_repo=_FakeFedRepo(peer),
        space_repo=_FakeSpaceRepo(),
        space_post_repo=_FakeSpacePostRepo(),
        space_task_repo=_Stub(),
        page_repo=_Stub(),
        sticky_repo=_Stub(),
        space_calendar_repo=_Stub(),
        gallery_repo=_Stub(),
        pending_decrypts=cache,
    )
    envelope = {
        "sync_id": "sync-tamper",
        "resource": "members",
        "space_id": "sp",
        "epoch": 0,
        "seq_start": 0,
        "seq_end": 0,
        "is_last": False,
        "encrypted_payload": "x:y",
    }
    envelope = await _sign_as_peer(kp, envelope)
    await r.on_chunk(serialise_chunk(envelope), from_instance="peer-a")
    # Not stashed (the failure is permanent, not race-recoverable).
    assert len(cache) == 0


# ── #648: a mesh host has no paired-peer row ──────────────────────────


async def test_on_chunk_verifies_mesh_host_via_space_host_identity_pk(
    receiver, peer_setup
):
    """A chunk from an UNPAIRED host verifies against the space's host key.

    This is #648's third layer. A member that joined over the mesh has no
    ``remote_instances`` row for the host, so the paired-peer lookup
    returns None and every chunk used to be discarded at DEBUG — the
    joiner ended up with the space stub, the content key and the media
    bytes but zero post rows. The key comes from the sealed invite and was
    only stored after ``derive_instance_id(pk) == host`` passed.
    """
    r, space_repo, _ = receiver
    _peer, kp = peer_setup
    host = "mesh-host-iid"
    space_repo.host_identity_pks["sp-mesh"] = kp.public_key.hex()
    space_repo.spaces["sp-mesh"] = SimpleNamespace(owner_instance_id=host)

    crypto = _FakeCrypto()
    _, ciphertext = await crypto.encrypt_chunk(
        space_id="sp-mesh",
        sync_id="sync-mesh",
        plaintext=orjson.dumps(
            {
                "records": [
                    {
                        "user_id": "u-mesh",
                        "role": "member",
                        "joined_at": "2026-04-18T00:00:00+00:00",
                    }
                ],
            }
        ),
    )
    envelope = await _sign_as_peer(
        kp,
        {
            "sync_id": "sync-mesh",
            "resource": "members",
            "space_id": "sp-mesh",
            "epoch": 0,
            "seq_start": 0,
            "seq_end": 0,
            "is_last": False,
            "encrypted_payload": ciphertext,
        },
    )

    await r.on_chunk(serialise_chunk(envelope), from_instance=host)

    # Verified and dispatched — the members resource persisted a row.
    assert space_repo.members, "chunk from a mesh host was still dropped"
    assert space_repo.members[0].user_id == "u-mesh"


async def test_on_chunk_rejects_mesh_chunk_from_a_non_host_instance(
    receiver, peer_setup
):
    """Only the household the stub names as host may sign the space's content.

    Otherwise any unpaired instance that named the space id would be
    verified against the host's key. It would fail the signature check,
    but the identity binding belongs here explicitly.
    """
    r, space_repo, _ = receiver
    _peer, kp = peer_setup
    space_repo.host_identity_pks["sp-mesh"] = kp.public_key.hex()
    space_repo.spaces["sp-mesh"] = SimpleNamespace(owner_instance_id="the-real-host")

    envelope = await _sign_as_peer(
        kp,
        {
            "sync_id": "sync-mesh",
            "resource": "members",
            "space_id": "sp-mesh",
            "epoch": 0,
            "seq_start": 0,
            "seq_end": 0,
            "is_last": False,
            "encrypted_payload": "x:y",
        },
    )

    await r.on_chunk(serialise_chunk(envelope), from_instance="some-other-household")

    assert space_repo.members == []


async def test_on_chunk_rejects_mesh_chunk_signed_by_the_wrong_key(
    receiver, peer_setup
):
    """A tampered/forged chunk still fails once the fallback key is used."""
    r, space_repo, _ = receiver
    _peer, _kp = peer_setup
    host = "mesh-host-iid"
    attacker = generate_identity_keypair()
    # The stub holds the REAL host key; the chunk is signed by someone else.
    real_host = generate_identity_keypair()
    space_repo.host_identity_pks["sp-mesh"] = real_host.public_key.hex()
    space_repo.spaces["sp-mesh"] = SimpleNamespace(owner_instance_id=host)

    envelope = await _sign_as_peer(
        attacker,
        {
            "sync_id": "sync-mesh",
            "resource": "members",
            "space_id": "sp-mesh",
            "epoch": 0,
            "seq_start": 0,
            "seq_end": 0,
            "is_last": False,
            "encrypted_payload": "x:y",
        },
    )

    await r.on_chunk(serialise_chunk(envelope), from_instance=host)

    assert space_repo.members == []


# ── #650: gallery persist failures must be visible ────────────────────


async def test_persist_album_failure_is_logged_not_swallowed(receiver, caplog):
    """A failing album insert must surface, not vanish.

    ``create_album`` is already ``ON CONFLICT DO NOTHING``, so a
    redelivered album never reaches the exception path — which means the
    old blanket ``except Exception: pass`` (commented "already exists")
    could only ever hide real failures. It hid #650 for as long as gallery
    sync has existed: a remotely-owned album violated
    ``owner_user_id REFERENCES users``, so members got image bytes and no
    rows.
    """
    r, space_repo, _ = receiver
    r._gallery_repo.create_album = AsyncMock(side_effect=RuntimeError("boom"))

    with caplog.at_level(logging.WARNING, logger="socialhome"):
        await r._persist_album(
            {"id": "al-1", "space_id": "sp-1", "name": "Holiday", "is_system": 0},
        )

    assert "gallery album al-1" in caplog.text


async def test_persist_gallery_item_failure_is_logged_not_swallowed(receiver, caplog):
    """Same for items — a missing parent album must not be silent."""
    r, _space_repo, _ = receiver
    r._gallery_repo.create_item = AsyncMock(side_effect=RuntimeError("boom"))

    with caplog.at_level(logging.WARNING, logger="socialhome"):
        await r._persist_gallery_item(
            {
                "id": "it-1",
                "album_id": "al-missing",
                "uploaded_by": "u-remote",
                "item_type": "photo",
                "url": "/api/media/a.webp",
                "thumbnail_url": "/api/media/t.webp",
                "width": 10,
                "height": 10,
            },
        )

    assert "gallery item it-1" in caplog.text


async def test_on_chunk_refuses_a_chunk_for_another_space(receiver, peer_setup):
    """F3 — a sync session is for ONE space. Nothing downstream of the
    signature check looked at which space the chunk claimed, so a
    provider streaming a session we opened for space A could write
    members, bans and every content row of space B."""
    r, space_repo, _ = receiver
    _peer, kp = peer_setup
    crypto = _FakeCrypto()
    plaintext = orjson.dumps({"records": [{"user_id": "u-1", "role": "admin"}]})
    _, ciphertext = await crypto.encrypt_chunk(
        space_id="sp-1",
        sync_id="sync-1",
        plaintext=plaintext,
    )
    envelope = await _sign_as_peer(
        kp,
        {
            "sync_id": "sync-1",
            "resource": "members",
            "space_id": "sp-1",
            "epoch": 0,
            "seq_start": 0,
            "seq_end": 1,
            "is_last": False,
            "encrypted_payload": ciphertext,
        },
    )
    await r.on_chunk(
        serialise_chunk(envelope),
        from_instance="peer-a",
        expected_space_id="sp-somewhere-else",
    )
    assert space_repo.members == []


async def test_on_chunk_accepts_a_chunk_matching_the_session_space(
    receiver,
    peer_setup,
):
    r, space_repo, _ = receiver
    _peer, kp = peer_setup
    crypto = _FakeCrypto()
    plaintext = orjson.dumps({"records": [{"user_id": "u-1", "role": "member"}]})
    _, ciphertext = await crypto.encrypt_chunk(
        space_id="sp-1",
        sync_id="sync-1",
        plaintext=plaintext,
    )
    envelope = await _sign_as_peer(
        kp,
        {
            "sync_id": "sync-1",
            "resource": "members",
            "space_id": "sp-1",
            "epoch": 0,
            "seq_start": 0,
            "seq_end": 1,
            "is_last": False,
            "encrypted_payload": ciphertext,
        },
    )
    await r.on_chunk(
        serialise_chunk(envelope),
        from_instance="peer-a",
        expected_space_id="sp-1",
    )
    assert len(space_repo.members) == 1


async def test_dispatch_keeps_a_hidden_anchor_post_out_of_the_joiners_feed(receiver):
    """A synced bazaar / calendar anchor lands with ``hidden_from_feed``.

    The provider now ships anchor posts (their listings reference them by
    FK); the joiner must store the flag with the row, or every unannounced
    listing would surface as a feed card on the household that joined.
    """
    r, _space_repo, post_repo = receiver
    await r._dispatch(
        "posts",
        "sp-1",
        [
            {
                "id": "p-anchor",
                "author": "u-1",
                "type": "text",
                "content": "listing card",
                "hidden_from_feed": True,
            },
            {"id": "p-shown", "author": "u-1", "type": "text", "content": "hi"},
        ],
    )
    by_id = {p.id: p for _sid, p in post_repo.saved}
    assert by_id["p-anchor"].hidden_from_feed is True
    assert by_id["p-shown"].hidden_from_feed is False


async def test_bazaar_catchup_save_failure_is_a_warning_naming_the_listing(
    bus, peer_setup, caplog
):
    """A listing the joiner cannot store is logged at WARNING, not DEBUG.

    Regression: the FK failure on an anchor-less listing was swallowed at
    DEBUG for every unannounced listing on every joiner, so the only
    trace was the writer's "1/N statements failed" count.
    """
    import sqlite3

    peer, _ = peer_setup
    kp_self = generate_identity_keypair()

    class _RefusingBazaarRepo:
        async def save_listing(self, listing):
            raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")

    r = SpaceSyncReceiver(
        bus=bus,
        encoder=FederationEncoder(kp_self.private_key),
        crypto=_FakeCrypto(),
        federation_repo=_FakeFedRepo(peer),
        space_repo=_FakeSpaceRepo(),
        space_post_repo=_FakeSpacePostRepo(),
        space_task_repo=_Stub(),
        page_repo=_Stub(),
        sticky_repo=_Stub(),
        space_calendar_repo=_Stub(),
        gallery_repo=_Stub(),
        bazaar_repo=_RefusingBazaarRepo(),
    )
    record = {
        "post_id": "p-listing",
        "seller_user_id": "u-1",
        "mode": "fixed",
        "title": "Lamp",
        "status": "active",
    }
    with caplog.at_level(
        logging.WARNING, logger="socialhome.federation.sync.space.receiver"
    ):
        await r._dispatch("bazaar", "sp-1", [record])
    warnings = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING and "bazaar catch-up" in rec.getMessage()
    ]
    assert warnings, caplog.text
    assert "p-listing" in warnings[0]
    assert "FOREIGN KEY" in warnings[0]
