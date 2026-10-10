"""Unit tests for :class:`SpaceSyncReceiver`."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock
from types import SimpleNamespace


import orjson
import pytest

from socialhome.crypto import generate_identity_keypair
from socialhome.domain.events import SpaceContentKeyImported, SpaceSyncComplete
from socialhome.domain.sticky import DEFAULT_STICKY_COLOR, MAX_STICKY_CONTENT_LENGTH
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
from socialhome.federation.sync.space.receiver import (
    HELD_BACK_LIMIT,
    HeldBack,
    SpaceSyncReceiver,
    _sticky_from_record,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.space_sync_watermark_repo import (
    SqliteSpaceSyncWatermarkRepo,
)
from socialhome.services.pending_decrypts_cache import PendingDecryptsCache


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
        # Unless a test says otherwise, ``peer-a`` hosts the space — its
        # chunks are taken whole (non-host providers are covered by the
        # authorship protocol tests).
        return self.spaces.get(space_id) or SimpleNamespace(
            id=space_id,
            owner_instance_id="peer-a",
            archived=False,
            archived_reason=None,
            dissolved=False,
        )

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

    async def get(self, post_id):
        return None

    async def add_comment(self, comment, *, space_id):
        self.comments.append(comment)
        return True


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

    async def create_item_in_space(self, item, *, space_id, bump_count=True):
        self.saved.append(("item", item))
        return True

    async def get_system_album(self, space_id):
        return None

    async def get_album(self, album_id):
        return None

    async def recount_items(self, album_id):
        return 0

    async def is_album_deleted(self, album_id, *, space_id):
        return False

    async def is_item_deleted(self, item_id, *, space_id):
        return False


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
    # The sync id rides along, so the requester can tell the provider the
    # stream landed and both sides free the session.
    assert captured[0].sync_id == "sync-1"


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
    space_repo.spaces["sp-mesh"] = SimpleNamespace(
        owner_instance_id=host, archived=False, archived_reason=None, dissolved=False
    )

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
    space_repo.spaces["sp-mesh"] = SimpleNamespace(
        owner_instance_id="the-real-host",
        archived=False,
        archived_reason=None,
        dissolved=False,
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
    space_repo.spaces["sp-mesh"] = SimpleNamespace(
        owner_instance_id=host, archived=False, archived_reason=None, dissolved=False
    )

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

    held_back = HeldBack()
    with caplog.at_level(logging.WARNING, logger="socialhome"):
        await r._persist_album(
            {"id": "al-1", "space_id": "sp-1", "name": "Holiday", "is_system": 0},
            "sp-1",
            from_host=True,
            held_back=held_back,
        )

    assert "gallery album al-1" in caplog.text
    # Nor does it count as stored: the stream is reported unclean.
    assert held_back.count == 1


async def test_persist_gallery_item_failure_is_logged_not_swallowed(receiver, caplog):
    """Same for items — a missing parent album must not be silent."""
    r, _space_repo, _ = receiver
    r._gallery_repo.create_item_in_space = AsyncMock(side_effect=RuntimeError("boom"))
    held_back = HeldBack()

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
            "sp-1",
            held_back,
        )

    assert "gallery item it-1" in caplog.text
    assert held_back.count == 1


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
        provider="peer-a",
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
        async def save_listing(self, listing, *, space_id):
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
        await r._dispatch("bazaar", "sp-1", [record], provider="peer-a")
    warnings = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING and "bazaar catch-up" in rec.getMessage()
    ]
    assert warnings, caplog.text
    assert "p-listing" in warnings[0]
    assert "FOREIGN KEY" in warnings[0]


def test_synced_post_record_keeps_a_validated_link_preview():
    """A sync record round-trips the author-built card (exporter → receiver);
    the receiver re-validates it and keeps only a local image reference."""
    from datetime import datetime, timezone

    from socialhome.domain.link_preview import LinkPreview
    from socialhome.domain.post import Post, PostType
    from socialhome.federation.sync.space.exporters.posts import _post_to_dict
    from socialhome.federation.sync.space.receiver import _post_from_record

    card = LinkPreview(
        url="https://example.com/", title="T", thumbnail_url="api/media/lp.webp"
    )
    post = Post(
        id="p-sync",
        author="u",
        type=PostType.TEXT,
        created_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        content="https://example.com/",
        link_preview=card,
    )
    record = _post_to_dict(post)
    assert record["link_preview"]["thumbnail_url"] == "api/media/lp.webp"
    back = _post_from_record(record)
    assert back is not None and back.link_preview == card
    record["link_preview"]["thumbnail_url"] = "https://tracker.example/x.png"
    assert _post_from_record(record).link_preview.thumbnail_url is None
    record["link_preview"] = {"url": "file:///etc/passwd", "title": "T"}
    assert _post_from_record(record).link_preview is None


# ─── Sticky snapshot records go through the shared field rules ─────────


def test_sticky_from_record_never_keeps_a_non_hex_color():
    s = _sticky_from_record(
        {
            "id": "st-1",
            "author": "u",
            "content": "x",
            "color": "url(https://evil.example/t.png)",
        },
        "sp-1",
    )
    assert s is not None and s.color == DEFAULT_STICKY_COLOR
    s = _sticky_from_record({"id": "st-2", "author": "u", "content": "x"}, "sp-1")
    assert s is not None and s.color == DEFAULT_STICKY_COLOR


def test_sticky_from_record_keeps_valid_hex_canonical():
    s = _sticky_from_record(
        {"id": "st-1", "author": "u", "content": "x", "color": "#abc"}, "sp-1"
    )
    assert s is not None and s.color == "#AABBCC"


def test_sticky_from_record_clamps_coords_and_survives_overflow():
    s = _sticky_from_record(
        {
            "id": "st-1",
            "author": "u",
            "content": "x",
            "position_x": 10**400,
            "position_y": -3.0,
        },
        "sp-1",
    )
    assert s is not None and (s.position_x, s.position_y) == (0.0, 0.0)


def test_sticky_from_record_truncates_content_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        s = _sticky_from_record(
            {"id": "st-1", "author": "u", "content": "‮" + "y" * 3000},
            "sp-1",
        )
    assert s is not None and s.content == "y" * MAX_STICKY_CONTENT_LENGTH
    assert any("truncat" in r.message for r in caplog.records)


def test_sticky_from_record_invisible_content_is_dropped():
    assert (
        _sticky_from_record({"id": "st-1", "author": "u", "content": "​\x00"}, "sp-1")
        is None
    )


# ── §25.6 incremental: the sentinel reports whether every chunk applied ──


async def _members_chunk(
    kp, sync_id: str, *, user: str = "u-1", index: int | None = None
) -> bytes:
    body: dict = {"records": [{"user_id": user, "role": "member", "joined_at": "2026"}]}
    if index is not None:
        body["chunk_index"] = index
    plaintext = orjson.dumps(body)
    _, ciphertext = await _FakeCrypto().encrypt_chunk(
        space_id="sp-1", sync_id=sync_id, plaintext=plaintext
    )
    envelope = {
        "sync_id": sync_id,
        "resource": "members",
        "space_id": "sp-1",
        "epoch": 0,
        "seq_start": 0,
        "seq_end": 1,
        "is_last": False,
        "encrypted_payload": ciphertext,
    }
    return serialise_chunk(await _sign_as_peer(kp, envelope))


async def _sentinel_frame(
    kp, sync_id: str, chunk_count: int | None, snapshot_seq: object = None
) -> bytes:
    sentinel = {
        "sync_id": sync_id,
        "resource": SENTINEL_RESOURCE,
        "space_id": "sp-1",
        "is_last": True,
    }
    if chunk_count is not None:
        sentinel["chunk_count"] = chunk_count
    if snapshot_seq is not None:
        sentinel["snapshot_seq"] = snapshot_seq
    return serialise_chunk(await _sign_as_peer(kp, sentinel))


async def _completion(bus, r, kp, sync_id, chunk_count) -> SpaceSyncComplete:
    captured: list[SpaceSyncComplete] = []
    bus.subscribe(SpaceSyncComplete, captured.append)
    await r.on_chunk(
        await _sentinel_frame(kp, sync_id, chunk_count), from_instance="peer-a"
    )
    assert len(captured) == 1
    return captured[0]


async def test_a_stream_whose_every_chunk_applied_completes_clean(
    bus, receiver, peer_setup
):
    r, _, _ = receiver
    _, kp = peer_setup
    await r.on_chunk(await _members_chunk(kp, "s-ok"), from_instance="peer-a")
    await r.on_chunk(
        await _members_chunk(kp, "s-ok", user="u-2"), from_instance="peer-a"
    )
    assert (await _completion(bus, r, kp, "s-ok", 2)).clean is True


async def test_a_chunk_that_never_arrived_makes_the_stream_unclean(
    bus, receiver, peer_setup
):
    """A chunk dropped on the way (a relay that could not open it) never
    reaches the receiver — the provider's signed count says so."""
    r, _, _ = receiver
    _, kp = peer_setup
    await r.on_chunk(await _members_chunk(kp, "s-gap"), from_instance="peer-a")
    assert (await _completion(bus, r, kp, "s-gap", 2)).clean is False


async def test_a_chunk_that_failed_to_apply_makes_the_stream_unclean(
    bus, receiver, peer_setup
):
    r, space_repo, _ = receiver
    _, kp = peer_setup
    space_repo.save_member = AsyncMock(side_effect=RuntimeError("FK failed"))
    await r.on_chunk(await _members_chunk(kp, "s-bad"), from_instance="peer-a")
    assert (await _completion(bus, r, kp, "s-bad", 1)).clean is False


async def test_a_tampered_chunk_makes_the_stream_unclean(bus, receiver, peer_setup):
    r, _, _ = receiver
    _, kp = peer_setup
    frame = orjson.loads(await _members_chunk(kp, "s-tamper"))
    frame["seq_end"] = 99
    await r.on_chunk(orjson.dumps(frame), from_instance="peer-a")
    assert (await _completion(bus, r, kp, "s-tamper", 1)).clean is False


async def test_a_chunk_still_waiting_for_its_key_makes_the_stream_unclean(
    bus, peer_setup
):
    peer, kp = peer_setup
    r = SpaceSyncReceiver(
        bus=bus,
        encoder=FederationEncoder(generate_identity_keypair().private_key),
        crypto=_MissingKeyCrypto(),
        federation_repo=_FakeFedRepo(peer),
        space_repo=_FakeSpaceRepo(),
        space_post_repo=_FakeSpacePostRepo(),
        space_task_repo=_Stub(),
        page_repo=_Stub(),
        sticky_repo=_Stub(),
        space_calendar_repo=_Stub(),
        gallery_repo=_Stub(),
        pending_decrypts=PendingDecryptsCache(bus=bus),
    )
    await r.on_chunk(await _members_chunk(kp, "s-wait"), from_instance="peer-a")
    assert (await _completion(bus, r, kp, "s-wait", 1)).clean is False


async def test_a_sentinel_overtaking_a_chunks_persist_is_not_clean(
    bus, receiver, peer_setup
):
    """A chunk counts once its records are stored, not when it arrives:
    relayed chunks are separate inbound events, so the sentinel can be
    handled while an earlier chunk is still persisting — and that persist
    may yet fail. The verdict must not run ahead of the database."""
    r, space_repo, _ = receiver
    _, kp = peer_setup
    gate = asyncio.Event()
    entered = asyncio.Event()
    stored = space_repo.save_member

    async def _slow_save(member):
        entered.set()
        await gate.wait()
        raise RuntimeError("database is locked")

    space_repo.save_member = _slow_save
    persisting = asyncio.create_task(
        r.on_chunk(await _members_chunk(kp, "s-race"), from_instance="peer-a")
    )
    await entered.wait()
    done = await _completion(bus, r, kp, "s-race", 1)
    gate.set()
    await persisting
    space_repo.save_member = stored
    assert done.clean is False


async def test_a_chunk_counts_only_once_stored(bus, receiver, peer_setup):
    """The same race with a persist that succeeds: still not clean — the
    sentinel saw the chunk unstored, so the next session re-streams it."""
    r, space_repo, _ = receiver
    _, kp = peer_setup
    gate = asyncio.Event()
    entered = asyncio.Event()
    stored = space_repo.save_member

    async def _slow_save(member):
        entered.set()
        await gate.wait()
        return await stored(member)

    space_repo.save_member = _slow_save
    persisting = asyncio.create_task(
        r.on_chunk(await _members_chunk(kp, "s-slow"), from_instance="peer-a")
    )
    await entered.wait()
    done = await _completion(bus, r, kp, "s-slow", 1)
    gate.set()
    await persisting
    assert done.clean is False
    # The late chunk leaves nothing behind for a stream already finished.
    assert "s-slow" not in r._health._streams


async def test_a_redelivered_chunk_counts_once_it_applies(bus, peer_setup):
    """A chunk stashed for its epoch key counts when the replay stores it."""
    peer, kp = peer_setup
    crypto = _MissingKeyCrypto()
    cache = PendingDecryptsCache(bus=bus)
    r = SpaceSyncReceiver(
        bus=bus,
        encoder=FederationEncoder(generate_identity_keypair().private_key),
        crypto=crypto,
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
    await r.on_chunk(await _members_chunk(kp, "s-key"), from_instance="peer-a")
    await bus.publish(SpaceContentKeyImported(space_id="sp-1", epoch=0))
    assert (await _completion(bus, r, kp, "s-key", 1)).clean is True


async def test_a_duplicated_chunk_cannot_stand_in_for_a_lost_one(
    bus, receiver, peer_setup
):
    """v_56: chunks carry their index inside the signed, encrypted payload;
    the receiver counts distinct indices, so chunk 0 delivered twice and
    chunk 1 lost is not two stored chunks."""
    r, _, _ = receiver
    _, kp = peer_setup
    chunk0 = await _members_chunk(kp, "s-dup", index=0)
    await r.on_chunk(chunk0, from_instance="peer-a")
    await r.on_chunk(chunk0, from_instance="peer-a")
    assert (await _completion(bus, r, kp, "s-dup", 2)).clean is False
    for i in range(2):
        await r.on_chunk(
            await _members_chunk(kp, "s-ok2", user=f"u-{i}", index=i),
            from_instance="peer-a",
        )
    assert (await _completion(bus, r, kp, "s-ok2", 2)).clean is True


async def test_held_back_streams_are_bounded(
    bus, receiver, peer_setup, caplog, monkeypatch
):
    """A record that never lands must not keep every session unclean: the
    HELD_BACK_LIMIT-th held-back-only stream in a row from one provider is
    reported clean (WARNING once); a clean stream resets the count."""
    r, _, _ = receiver
    _, kp = peer_setup

    async def _held_dispatch(*_a, **_kw):
        return False

    monkeypatch.setattr(SpaceSyncReceiver, "_dispatch", _held_dispatch)
    verdicts = []
    with caplog.at_level(logging.WARNING, logger="socialhome"):
        for n in range(HELD_BACK_LIMIT + 1):
            sid = f"s-held-{n}"
            await r.on_chunk(await _members_chunk(kp, sid), from_instance="peer-a")
            verdicts.append((await _completion(bus, r, kp, sid, 1)).clean)
    assert verdicts == [False] * (HELD_BACK_LIMIT - 1) + [True, False]
    assert sum("never landed" in rec.message for rec in caplog.records) == 1
    # A failure is never forgiven, however long the streak.
    monkeypatch.undo()
    await r.on_chunk(await _members_chunk(kp, "s-gap3"), from_instance="peer-a")
    assert (await _completion(bus, r, kp, "s-gap3", 2)).clean is False


async def test_an_archive_lifted_mid_stream_records_no_echo(bus, peer_setup):
    """The unarchive dropped the echo so the next session is full; the
    stream that was running must not write its snapshot back."""
    peer, kp = peer_setup
    applied = _AppliedSeqs()
    r = _echo_receiver(bus, peer, applied)
    spaces = r._space_repo
    spaces.spaces["sp-1"] = SimpleNamespace(
        id="sp-1",
        owner_instance_id="peer-a",
        archived=True,
        archived_reason=None,
        dissolved=False,
    )
    await r.on_chunk(await _members_chunk(kp, "s-arch"), from_instance="peer-a")
    spaces.spaces["sp-1"].archived = False
    captured: list[SpaceSyncComplete] = []
    bus.subscribe(SpaceSyncComplete, captured.append)
    await r.on_chunk(await _sentinel_frame(kp, "s-arch", 1, 41), from_instance="peer-a")
    assert applied.recorded == []
    # Still clean toward the provider: what it shipped was stored.
    assert [e.clean for e in captured] == [True]
    # A stream that stays archived (or unarchived) throughout records it.
    await r.on_chunk(await _members_chunk(kp, "s-arch2"), from_instance="peer-a")
    await r.on_chunk(
        await _sentinel_frame(kp, "s-arch2", 1, 42), from_instance="peer-a"
    )
    assert applied.recorded == [("sp-1", "peer-a", 42)]


async def test_a_sentinel_without_a_count_is_never_clean(bus, receiver, peer_setup):
    """An older provider sends no count — it keeps no watermark anyway."""
    r, _, _ = receiver
    _, kp = peer_setup
    await r.on_chunk(await _members_chunk(kp, "s-old"), from_instance="peer-a")
    assert (await _completion(bus, r, kp, "s-old", None)).clean is False


# ── §25.6 echo (migration 0087): a clean stream records its snapshot ─────


class _AppliedSeqs:
    def __init__(self, *, fail: bool = False) -> None:
        self.recorded: list[tuple[str, str, int]] = []
        self._fail = fail

    async def record_applied(self, space_id: str, instance_id: str, seq: int):
        if self._fail:
            raise RuntimeError("database is locked")
        self.recorded.append((space_id, instance_id, seq))


def _echo_receiver(bus, peer, applied) -> SpaceSyncReceiver:
    return SpaceSyncReceiver(
        bus=bus,
        encoder=FederationEncoder(generate_identity_keypair().private_key),
        crypto=_FakeCrypto(),
        federation_repo=_FakeFedRepo(peer),
        space_repo=_FakeSpaceRepo(),
        space_post_repo=_FakeSpacePostRepo(),
        space_task_repo=_Stub(),
        page_repo=_Stub(),
        sticky_repo=_Stub(),
        space_calendar_repo=_Stub(),
        gallery_repo=_Stub(),
        applied_seqs=applied,
    )


async def test_a_clean_stream_records_the_providers_snapshot(bus, peer_setup):
    peer, kp = peer_setup
    applied = _AppliedSeqs()
    r = _echo_receiver(bus, peer, applied)
    await r.on_chunk(await _members_chunk(kp, "s-echo"), from_instance="peer-a")
    await r.on_chunk(await _sentinel_frame(kp, "s-echo", 1, 41), from_instance="peer-a")
    assert applied.recorded == [("sp-1", "peer-a", 41)]


@pytest.mark.parametrize("snapshot", [None, -1, "41", True])
async def test_no_valid_snapshot_records_nothing(bus, peer_setup, snapshot):
    peer, kp = peer_setup
    applied = _AppliedSeqs()
    r = _echo_receiver(bus, peer, applied)
    await r.on_chunk(
        await _sentinel_frame(kp, "s-none", 0, snapshot), from_instance="peer-a"
    )
    assert applied.recorded == []


async def test_an_unclean_stream_records_nothing(bus, peer_setup):
    peer, kp = peer_setup
    applied = _AppliedSeqs()
    r = _echo_receiver(bus, peer, applied)
    await r.on_chunk(await _members_chunk(kp, "s-gap2"), from_instance="peer-a")
    await r.on_chunk(await _sentinel_frame(kp, "s-gap2", 2, 41), from_instance="peer-a")
    assert applied.recorded == []


async def test_a_failed_echo_write_still_completes_the_stream(bus, peer_setup):
    """Bookkeeping is fail-soft: the requester still confirms (the next
    BEGIN then carries the older echo — more data, never less)."""
    peer, kp = peer_setup
    r = _echo_receiver(bus, peer, _AppliedSeqs(fail=True))
    captured: list[SpaceSyncComplete] = []
    bus.subscribe(SpaceSyncComplete, captured.append)
    await r.on_chunk(await _sentinel_frame(kp, "s-fail", 0, 7), from_instance="peer-a")
    assert [e.clean for e in captured] == [True]


async def test_an_out_of_range_snapshot_stores_nothing_and_breaks_no_batch(
    bus, peer_setup, db
):
    """A peer's signed sentinel naming ``snapshot_seq >= 2**63`` (no SQLite
    INTEGER holds it) is no echo: nothing is written, and a write coalesced
    into the same batch still lands."""
    peer, kp = peer_setup
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-1','S','peer-a','anna','ab')"
    )
    await db.enqueue(
        "INSERT INTO space_instances(space_id, instance_id) VALUES('sp-1','peer-a')"
    )
    repo = SqliteSpaceSyncWatermarkRepo(db)
    await repo.record_applied("sp-1", "peer-a", 7)
    r = _echo_receiver(bus, peer, repo)
    captured: list[SpaceSyncComplete] = []
    bus.subscribe(SpaceSyncComplete, captured.append)
    await asyncio.gather(
        r.on_chunk(
            await _sentinel_frame(kp, "s-big", 0, 2**63), from_instance="peer-a"
        ),
        db.enqueue(
            "INSERT INTO space_instances(space_id, instance_id) VALUES('sp-1','b')"
        ),
    )
    assert [e.clean for e in captured] == [True]
    assert await repo.applied_seq("sp-1", "peer-a") == 7
    assert (
        await db.fetchone("SELECT 1 FROM space_instances WHERE instance_id='b'")
        is not None
    )
