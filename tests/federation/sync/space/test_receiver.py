"""Unit tests for :class:`SpaceSyncReceiver`: the chunk path (verify,
decrypt, admit, dispatch) and the stream verdict. The per-resource persist
paths are tested in ``test_receive_*.py``."""

from __future__ import annotations

import asyncio
import base64
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import orjson
import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.domain.events import SpaceContentKeyImported, SpaceSyncComplete
from socialhome.domain.space_media_fields import (
    SPACE_MEDIA_ITEMS,
    item_media_urls,
)
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.owner_bound_id import (
    SPACE_CHAT_MESSAGE_KIND,
    mint_owner_bound_id,
)
from socialhome.federation.sync.space.exporter import SENTINEL_RESOURCE, serialise_chunk
from socialhome.federation.sync.space.receive_bazaar import (
    _bazaar_listing_from_record,
)
from socialhome.federation.sync.space.receive_calendar import (
    _calendar_from_record,
)
from socialhome.federation.sync.space.receive_gallery import (
    _gallery_item_from_record,
)
from socialhome.federation.sync.space.receive_posts import (
    _comment_from_record,
    _post_from_record,
)
from socialhome.federation.sync.space.receiver import SpaceSyncReceiver
from socialhome.federation.sync.space.stream_health import (
    HELD_BACK_LIMIT,
    PERSIST_FAILURE_LIMIT,
    HeldBack,
)
from socialhome.repositories.space_sync_watermark_repo import (
    SqliteSpaceSyncWatermarkRepo,
)
from socialhome.services.pending_decrypts_cache import PendingDecryptsCache
from tests.federation.sync.space.receiver_fakes import (
    _FakeCrypto,
    _FakeFedRepo,
    _FakeSpacePostRepo,
    _FakeSpaceRepo,
    _send,
    _sign_as_peer,
    _Stub,
)
from tests.space_media_items import expected_urls, synthetic_record


async def test_on_chunk_persists_members(receiver, peer_setup):
    r, space_repo, _ = receiver
    _peer, kp = peer_setup
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
    _peer, kp = peer_setup
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
    _peer, kp = peer_setup
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
    _peer, _kp = peer_setup
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
    r, _space_repo, _ = receiver
    _peer, kp = peer_setup
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


class _MissingKeyCrypto:
    """Crypto stub that raises 'missing epoch' on first decrypt, then
    succeeds on the second call — simulates a key import in-between."""

    def __init__(self) -> None:
        self.attempts = 0

    async def encrypt_chunk(self, *, space_id, sync_id, plaintext):

        return 5, base64.urlsafe_b64encode(plaintext).decode("ascii")

    async def decrypt_chunk(self, *, space_id, epoch, sync_id, ciphertext):

        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError(
                f"SpaceContentEncryption: missing epoch {epoch} for space {space_id!r}",
            )
        return base64.urlsafe_b64decode(ciphertext)


async def test_decrypt_missing_epoch_stashes_in_cache_and_replays(
    bus,
    peer_setup,
    caplog,
):
    """The first chunk arrives before the key — decrypt raises with
    'missing epoch', the receiver stashes the redeliver in the cache,
    and the matching SpaceContentKeyImported event triggers a replay
    that succeeds. End state: row was persisted on the second try."""

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

    # First arrival — key missing, gets stashed. An expected, handled race:
    # one INFO line that says so, no traceback (it read as a crash in the
    # log of a host that came back before the rekey it had missed).
    with caplog.at_level(logging.INFO):
        await r.on_chunk(serialise_chunk(envelope), from_instance="peer-a")
    stashed = [x for x in caplog.records if "stashing for replay" in x.getMessage()]
    assert len(stashed) == 1
    assert stashed[0].exc_info is None
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


async def test_an_unknown_resource_we_cannot_open_still_counts(bus, peer_setup):
    """Forward compatibility: a newer provider's resource that this
    household cannot decrypt is dropped by rule and counted as one chunk —
    it does not make every stream from that provider unclean."""
    peer, kp = peer_setup

    class _NoOpenCrypto(_FakeCrypto):
        async def decrypt_chunk(self, *, space_id, epoch, sync_id, ciphertext):
            if ciphertext == "opaque":
                raise ValueError("unknown scheme")
            return await super().decrypt_chunk(
                space_id=space_id, epoch=epoch, sync_id=sync_id, ciphertext=ciphertext
            )

    r = SpaceSyncReceiver(
        bus=bus,
        encoder=FederationEncoder(generate_identity_keypair().private_key),
        crypto=_NoOpenCrypto(),
        federation_repo=_FakeFedRepo(peer),
        space_repo=_FakeSpaceRepo(),
        space_post_repo=_FakeSpacePostRepo(),
        space_task_repo=_Stub(),
        page_repo=_Stub(),
        sticky_repo=_Stub(),
        space_calendar_repo=_Stub(),
        gallery_repo=_Stub(),
    )
    await r.on_chunk(await _members_chunk(kp, "s-opq", index=0), from_instance="peer-a")
    frame = orjson.loads(await _members_chunk(kp, "s-opq", index=1))
    frame.update(resource="from_the_future", encrypted_payload="opaque")
    del frame["signatures"]
    await r.on_chunk(
        serialise_chunk(await _sign_as_peer(kp, frame)), from_instance="peer-a"
    )
    assert (await _completion(bus, r, kp, "s-opq", 2)).clean is True
    # A KNOWN resource that fails to decrypt is a failure, as ever.
    bad = orjson.loads(await _members_chunk(kp, "s-bad2", index=0))
    bad["encrypted_payload"] = "opaque"
    del bad["signatures"]
    await r.on_chunk(
        serialise_chunk(await _sign_as_peer(kp, bad)), from_instance="peer-a"
    )
    assert (await _completion(bus, r, kp, "s-bad2", 1)).clean is False


async def test_held_back_streams_are_bounded(
    bus, receiver, peer_setup, caplog, monkeypatch
):
    """A record that never lands must not keep every session unclean: the
    HELD_BACK_LIMIT-th held-back-only stream in a row from one provider is
    reported clean (WARNING once); a clean stream resets the count."""
    r, _, _ = receiver
    _, kp = peer_setup

    async def _held_apply(*_a, **_kw):
        outcome = HeldBack()
        outcome.add()
        return outcome

    async def _held_stream(sid: str) -> bool:
        await r.on_chunk(await _members_chunk(kp, sid), from_instance="peer-a")
        return (await _completion(bus, r, kp, sid, 1)).clean

    monkeypatch.setattr(SpaceSyncReceiver, "_apply", _held_apply)
    verdicts = []
    with caplog.at_level(logging.WARNING, logger="socialhome"):
        for n in range(HELD_BACK_LIMIT + 1):
            verdicts.append(await _held_stream(f"s-held-{n}"))
    assert verdicts == [False] * (HELD_BACK_LIMIT - 1) + [True, False]
    assert sum("never landed" in rec.message for rec in caplog.records) == 1
    # "In a row": an unclean stream (a lost chunk) breaks the run, so the
    # count starts over after it.
    await r.on_chunk(await _members_chunk(kp, "s-gap3"), from_instance="peer-a")
    assert (await _completion(bus, r, kp, "s-gap3", 2)).clean is False
    assert [await _held_stream(f"s-again-{n}") for n in range(2)] == [False, False]


async def test_a_failed_persist_is_bounded_apart_from_held_back(
    bus, receiver, peer_setup, monkeypatch, caplog
):
    """A persist that failed (logged, not raised) is unclean — it never
    counts toward the held-back bound, but has its own, higher one."""
    r, _, _ = receiver
    _, kp = peer_setup

    async def _failed_apply(*_a, **_kw):
        outcome = HeldBack()
        outcome.fail()
        return outcome

    monkeypatch.setattr(SpaceSyncReceiver, "_apply", _failed_apply)
    verdicts = []
    with caplog.at_level(logging.WARNING, logger="socialhome"):
        for n in range(PERSIST_FAILURE_LIMIT + 1):
            sid = f"s-fail-{n}"
            await r.on_chunk(await _members_chunk(kp, sid), from_instance="peer-a")
            verdicts.append((await _completion(bus, r, kp, sid, 1)).clean)
    # Not forgiven as "held" (3) — only after its own, higher bound, so a
    # row the database refuses for good cannot force full re-streams for
    # ever.
    assert verdicts == [False] * (PERSIST_FAILURE_LIMIT - 1) + [True, False]
    assert PERSIST_FAILURE_LIMIT > HELD_BACK_LIMIT
    assert sum("failed to store" in rec.message for rec in caplog.records) == 1


async def test_in_a_numbered_stream_only_indices_count(bus, receiver, peer_setup):
    """Once a stream numbers its chunks (v_56), an unnumbered chunk in it
    cannot make up the count; an unknown resource is decrypted, so its
    index counts like any other."""
    r, _, _ = receiver
    _, kp = peer_setup
    await r.on_chunk(await _members_chunk(kp, "s-mix", index=0), from_instance="peer-a")
    await r.on_chunk(await _members_chunk(kp, "s-mix"), from_instance="peer-a")
    assert (await _completion(bus, r, kp, "s-mix", 2)).clean is False

    await r.on_chunk(await _members_chunk(kp, "s-new", index=0), from_instance="peer-a")
    frame = orjson.loads(await _members_chunk(kp, "s-new", index=1))
    frame["resource"] = "some_future_resource"
    del frame["signatures"]
    await r.on_chunk(
        serialise_chunk(await _sign_as_peer(kp, frame)), from_instance="peer-a"
    )
    assert (await _completion(bus, r, kp, "s-new", 2)).clean is True


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


async def _empty_members_chunk(kp, space_id="sp-mesh"):
    crypto = _FakeCrypto()
    _, ciphertext = await crypto.encrypt_chunk(
        space_id=space_id,
        sync_id="sync-adm",
        plaintext=orjson.dumps({"records": []}),
    )
    return serialise_chunk(
        await _sign_as_peer(
            kp,
            {
                "sync_id": "sync-adm",
                "resource": "members",
                "space_id": space_id,
                "epoch": 0,
                "seq_start": 0,
                "seq_end": 0,
                "is_last": False,
                "encrypted_payload": ciphertext,
            },
        )
    )


@pytest.mark.parametrize("recorded", ["own", "none", "other"])
async def test_on_chunk_verifies_an_unpaired_member_provider_by_its_recorded_key(
    receiver, recorded
):
    """A household that is not the host and not paired with us is verified
    only against the key recorded on its own ``space_instances`` row for
    this space — and only when that key derives to it."""
    r, space_repo, _ = receiver
    admin = generate_identity_keypair()
    admin_id = derive_instance_id(admin.public_key)
    space_repo.spaces["sp-mesh"] = SimpleNamespace(
        owner_instance_id="the-owner",
        archived=False,
        archived_reason=None,
        dissolved=False,
    )
    if recorded == "own":
        space_repo.instance_pks[("sp-mesh", admin_id)] = admin.public_key.hex()
    elif recorded == "other":
        # A key that does not derive to the sender is never used.
        space_repo.instance_pks[("sp-mesh", admin_id)] = (
            generate_identity_keypair().public_key.hex()
        )
    await r.on_chunk(await _empty_members_chunk(admin), from_instance=admin_id)
    stream = r._health._streams.get("sync-adm")
    failed = bool(stream is not None and stream.failed)
    assert failed is (recorded != "own")


class _Heard:
    def __init__(self) -> None:
        self.spaces: list[str] = []

    def heard(self, space_id: str) -> None:
        self.spaces.append(space_id)


async def test_an_acting_hosts_empty_session_marks_the_host_heard(receiver, peer_setup):
    """An up-to-date incremental session from the host sends back only its
    sentinel — that answer must count, or a quiet space always falls back
    to a helper."""
    r, _, _ = receiver
    _, kp = peer_setup
    contact = _Heard()
    r.attach_host_contact(contact)
    await r.on_chunk(await _sentinel_frame(kp, "s-q", 0), from_instance="peer-a")
    assert contact.spaces == ["sp-1"]
    await r.on_chunk(await _members_chunk(kp, "s-c"), from_instance="peer-a")
    assert contact.spaces == ["sp-1", "sp-1"]


async def test_a_plain_members_chunk_does_not_mark_the_host_heard(receiver, peer_setup):
    r, space_repo, _ = receiver
    _, kp = peer_setup
    space_repo.spaces["sp-1"] = SimpleNamespace(
        id="sp-1",
        owner_instance_id="host-elsewhere",
        archived=False,
        archived_reason=None,
        dissolved=False,
    )
    contact = _Heard()
    r.attach_host_contact(contact)
    await r.on_chunk(await _sentinel_frame(kp, "s-m", 0), from_instance="peer-a")
    await r.on_chunk(await _members_chunk(kp, "s-m2"), from_instance="peer-a")
    assert contact.spaces == []


class _Streams:
    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []

    def stream_started(self, space_id: str, provider: str) -> None:
        self.started.append((space_id, provider))


async def test_the_first_chunk_of_a_stream_tells_the_sync_status(receiver, peer_setup):
    """Once per stream (its first chunk), never per chunk, and never for a
    sentinel-only (quiet) session."""
    r, _, _ = receiver
    _, kp = peer_setup
    streams = _Streams()
    r.attach_stream_watch(streams)
    await r.on_chunk(await _sentinel_frame(kp, "s-q", 0), from_instance="peer-a")
    assert streams.started == []
    await r.on_chunk(await _members_chunk(kp, "s-c"), from_instance="peer-a")
    await r.on_chunk(await _members_chunk(kp, "s-c"), from_instance="peer-a")
    assert streams.started == [("sp-1", "peer-a")]


async def test_a_failing_stream_watch_never_costs_the_chunk(receiver, peer_setup):
    r, _, _ = receiver
    _, kp = peer_setup

    class _Boom:
        def stream_started(self, space_id: str, provider: str) -> None:
            raise RuntimeError("boom")

    r.attach_stream_watch(_Boom())
    await r.on_chunk(await _members_chunk(kp, "s-b"), from_instance="peer-a")
    assert not r._health._streams["s-b"].failed


async def test_a_forged_chunk_does_not_mark_the_host_heard(receiver):
    r, _, _ = receiver
    contact = _Heard()
    r.attach_host_contact(contact)
    stranger = generate_identity_keypair()
    await r.on_chunk(await _sentinel_frame(stranger, "s-f", 0), from_instance="peer-a")
    assert contact.spaces == []


async def test_missing_outer_fields_drops(setup):
    """Envelope without sync_id / resource / space_id → drop."""
    r, c, kp = setup
    envelope = {
        "sync_id": "",  # empty
        "resource": "posts",
        "space_id": "sp-1",
    }
    enc = FederationEncoder(kp.private_key)
    bytes_to_sign = orjson.dumps(envelope)
    envelope["signatures"] = enc.sign_envelope_all(
        bytes_to_sign,
        suite="ed25519",
    )
    await r.on_chunk(serialise_chunk(envelope), from_instance="peer-a")
    assert c.posts == []


async def test_decrypt_failure_drops(setup, monkeypatch):
    """If decryption raises, the chunk is logged + dropped."""
    r, c, kp = setup

    async def _bad_decrypt(*, space_id, epoch, sync_id, ciphertext):
        raise RuntimeError("wrong key")

    monkeypatch.setattr(r._crypto, "decrypt_chunk", _bad_decrypt)
    await _send(
        r,
        kp,
        "posts",
        [
            {"id": "p-1", "author": "u-1", "type": "text"},
        ],
    )
    assert c.posts == []


class _ChatSink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list, str]] = []

    async def apply_sync_records(self, space_id, records, *, provider):
        self.calls.append((space_id, list(records), provider))
        return self.held

    held = 0


def _chat_record(owner: str = "u-a", space_id: str = "sp-1") -> dict:
    mid = mint_owner_bound_id(
        SPACE_CHAT_MESSAGE_KIND, space_id=space_id, owner_user_id=owner
    )
    return {"id": mid, "message_id": mid, "author_user_id": owner, "content": "hi"}


async def test_chat_records_reach_the_chat_sink(setup):
    r, _collector, kp = setup
    sink = _ChatSink()
    r.attach_chat_sink(sink)
    good = _chat_record()
    await _send(r, kp, "chat_messages", [good])
    assert sink.calls == [("sp-1", [good], "peer-a")]


async def test_a_chat_record_claiming_another_authors_id_never_reaches_the_sink(
    setup,
):
    r, _collector, kp = setup
    sink = _ChatSink()
    r.attach_chat_sink(sink)
    squatted = {**_chat_record("u-a"), "author_user_id": "u-b"}
    good = _chat_record("u-b")
    await _send(r, kp, "chat_messages", [squatted, good])
    assert sink.calls == [("sp-1", [good], "peer-a")]


async def test_a_chat_record_held_for_its_seat_keeps_the_chunk_unsettled(setup):
    """The chat sink held a message for a seat that has not reached us: the
    chunk is not settled, so the stream it came in is reported unclean."""
    r, _collector, _kp = setup
    sink = _ChatSink()
    r.attach_chat_sink(sink)
    assert await r._dispatch(
        "chat_messages", "sp-1", [_chat_record()], provider="peer-a"
    )
    sink.held = 1
    assert not await r._dispatch(
        "chat_messages", "sp-1", [_chat_record()], provider="peer-a"
    )


async def test_chat_records_are_dropped_without_a_sink(setup):
    r, _collector, kp = setup
    await _send(r, kp, "chat_messages", [_chat_record()])  # no raise


# ─── Registry tripwire: every synced file field is sanitized ─────────────


#: Each registry item's §25.6 record parser, with the non-file fields a
#: record needs. A new registry item fails here until it is listed.
_PARSERS = {
    "posts": lambda r: _post_from_record(
        {
            **r,
            "id": "p",
            "author": "u",
            "type": "file",
            "file_meta": {
                **r["file_meta"],
                "mime_type": "application/pdf",
                "original_name": "f.pdf",
                "size_bytes": 1,
            },
            "link_preview": {
                **r["link_preview"],
                "url": "https://example.org/",
                "title": "T",
            },
        }
    ),
    "comments": lambda r: _comment_from_record(
        {**r, "id": "c", "post_id": "p", "author": "u", "type": "image"}
    ),
    "calendar": lambda r: _calendar_from_record(
        {
            **r,
            "id": "e",
            "calendar_id": "sp",
            "summary": "S",
            "created_by": "u",
            "start": "2026-01-01T10:00:00+00:00",
            "end": "2026-01-01T11:00:00+00:00",
        },
        provider="peer",
    ),
    "gallery": lambda r: _gallery_item_from_record({**r, "id": "g"}, "al"),
    "bazaar": lambda r: _bazaar_listing_from_record(
        {**r, "post_id": "p", "seller_user_id": "u", "mode": "fixed", "title": "T"},
        "sp",
    ),
}
#: Named exclusion: a page keeps its HOST's names byte for byte (v_82); a
#: member never reads them as its own files — it maps each to a
#: page-scoped ``sp-`` name at read time (``space_page_media_name``).
_NOT_SANITIZED_ON_SYNC = frozenset({"pages"})


def _reserved(record: object) -> object:
    if isinstance(record, dict):
        return {k: _reserved(v) for k, v in record.items()}
    if isinstance(record, list):
        return [_reserved(v) for v in record]
    if isinstance(record, str):
        return record.replace("api/media/", "api/media/dm-")
    return record


def test_every_registry_item_has_a_record_parser():
    assert set(_PARSERS) | _NOT_SANITIZED_ON_SYNC == set(SPACE_MEDIA_ITEMS)


@pytest.mark.parametrize("resource", sorted(_PARSERS))
def test_a_synced_record_keeps_every_registered_space_file(resource):
    item = _PARSERS[resource](synthetic_record(resource))
    assert item is not None
    assert item_media_urls(resource, item) == expected_urls(resource, stored_only=True)


@pytest.mark.parametrize("resource", sorted(_PARSERS))
def test_a_synced_record_drops_a_reserved_name_in_every_registered_field(resource):
    """A DM's (``dm-``) name in any file field of a §25.6 record never
    lands as a space item's file."""
    item = _PARSERS[resource](_reserved(synthetic_record(resource)))
    assert item is not None
    assert item_media_urls(resource, item) == []
