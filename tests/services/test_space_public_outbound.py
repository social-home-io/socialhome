"""Tests for :class:`SpacePublicOutbound` (Phase 5a2 producer).

A seed-holding household relays a PUBLIC/GLOBAL space post to the GFS as
an *encrypted, authority-signed* envelope — the GFS (and any relay) sees
only ``{space_id, epoch, encrypted_payload, authority_sig, ...}``, never
the post content or author. Households without the seed, non-public
spaces, and inbound-driven (loop) events do not relay.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_POST_PUBLIC,
    strip_authority_sig_fields,
    verify_authority_event,
)
from socialhome.crypto import (
    b64url_decode,
    derive_user_id,
    generate_identity_keypair,
    generate_space_keypair,
    verify_ed25519,
)
from socialhome.services.space_public_author import (
    author_signing_bytes,
    build_signed_author_inner,
    verify_signed_author_inner,
)
from tests.services.test_space_public_author import _v25_author_signing_bytes
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import CommentDeleted, PostDeleted, SpacePostCreated
from socialhome.domain.space_item import (
    AUTHORITY_KIND_FIELD,
    AUTHORITY_KIND_REMOVAL,
    ITEM_SIZE_BUCKETS,
    PAD_FIELD,
)
from socialhome.federation.owner_bound_id import SPACE_POST_KIND, mint_owner_bound_id
from socialhome.crypto import derive_instance_id
from socialhome.domain.space import SpaceFeatureAccess
from socialhome.services.space_public_authority import is_approved
from socialhome.domain.post import Comment, CommentType, Post, PostType
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_key_repo import SqliteSpaceKeyRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_crypto_service import SpaceContentEncryption
from socialhome.services.space_public_outbound import SpacePublicOutbound
from types import SimpleNamespace
from socialhome.domain.space import SpaceMember
from socialhome.services.space_writer_cert_service import SpaceWriterCertService
from socialhome.writer_cert import verify_writer_cert
from socialhome.domain.writer_cert import WriterCert
from socialhome.writer_cert import sign_writer_cert


def _unpadded(inner: dict) -> dict:
    """The decrypted inner without its size padding (``_pad``)."""
    return {k: v for k, v in inner.items() if k != PAD_FIELD}


class _CaptureGfs:
    """Stub for gfs_connection_service.publish_space_event — records calls."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def publish_space_event(self, *, space_id, event_type, payload) -> int:
        self.calls.append(
            {
                "space_id": space_id,
                "event_type": event_type,
                "payload": payload,
            }
        )
        return 1


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    own_kp = generate_identity_keypair()
    own_iid = "alpha.home"
    # Author user local to this household.
    author_user_id = derive_user_id(own_kp.public_key, "alice")
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES(?, 'alice', 'Alice', 'active')",
        (author_user_id,),
    )
    kek = KeyManager.from_data_dir(tmp_dir)
    space_repo = SqliteSpaceRepo(db, key_manager=kek)
    key_repo = SqliteSpaceKeyRepo(db)
    crypto = SpaceContentEncryption(key_repo, kek)
    user_repo = SqliteUserRepo(db)
    bus = EventBus()
    gfs = _CaptureGfs()

    async def _make_space(
        space_id: str,
        stype: SpaceType,
        *,
        with_seed: bool,
        join_mode: JoinMode = JoinMode.OPEN,
        allow_subscribers: bool = True,
    ):
        skp = generate_space_keypair()
        await space_repo.save(
            Space(
                id=space_id,
                name="S",
                owner_instance_id=own_iid,
                owner_username="alice",
                identity_public_key=skp.public_key.hex(),
                config_sequence=0,
                features=SpaceFeatures(allow_subscribers=allow_subscribers),
                space_type=stype,
                join_mode=join_mode,
            )
        )
        if with_seed:
            await space_repo.set_space_seed(space_id, skp.private_key)
        await crypto.initialise_for_space(space_id)
        return skp

    sub = SpacePublicOutbound(
        bus=bus,
        space_repo=space_repo,
        space_crypto=crypto,
        user_repo=user_repo,
        gfs_service=gfs,
    )
    sub.attach_identity(
        own_instance_id=own_iid,
        own_instance_public_key=own_kp.public_key,
        own_identity_seed=own_kp.private_key,
    )
    sub.wire()
    return {
        "db": db,
        "bus": bus,
        "gfs": gfs,
        "crypto": crypto,
        "space_repo": space_repo,
        "make_space": _make_space,
        "author_user_id": author_user_id,
        "own_iid": own_iid,
        "own_pk": own_kp.public_key,
        "own_seed": own_kp.private_key,
        "sub": sub,
    }


def _post(author: str) -> Post:
    return Post(
        id="post-1",
        author=author,
        type=PostType.TEXT,
        content="hello public space",
        created_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
    )


async def test_public_post_relayed_encrypted_and_authority_signed(env):
    skp = await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    await env["bus"].publish(
        SpacePostCreated(post=_post(env["author_user_id"]), space_id="sp-pub")
    )
    assert len(env["gfs"].calls) == 1
    call = env["gfs"].calls[0]
    assert call["event_type"] == AUTHORITY_EVENT_SPACE_POST_PUBLIC
    assert "from_instance" not in call
    envelope = call["payload"]
    # GFS-blind: wire envelope carries ONLY routing + ciphertext + sig.
    assert set(envelope) == {
        "space_id",
        "epoch",
        "encrypted_payload",
        "authority_sig",
        "authority_sig_suite",
    }
    # No plaintext content / author leaks into the envelope.
    blob = json.dumps(envelope)
    assert "hello public space" not in blob
    assert env["author_user_id"] not in blob
    # Authority signature verifies against the space public key.
    assert verify_authority_event(
        event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
        space_id="sp-pub",
        payload=strip_authority_sig_fields(envelope),
        authority_sig=envelope["authority_sig"],
        authority_sig_suite=envelope["authority_sig_suite"],
        space_public_key=skp.public_key,
    )
    # Decrypt the inner payload and confirm author_pk + post_id present.
    pt = await env["crypto"].decrypt(
        "sp-pub", envelope["epoch"], envelope["encrypted_payload"]
    )
    inner = json.loads(pt)
    assert inner["post_id"] == "post-1"
    assert inner["author_user_id"] == env["author_user_id"]
    assert inner["author_pk"] == env["own_pk"].hex()
    assert inner["content"] == "hello public space"
    # Per-author signature is present INSIDE the encrypted inner payload and
    # verifies against author_pk over the canonical signing bytes.
    assert "author_sig" in inner
    assert verify_ed25519(
        env["own_pk"],
        author_signing_bytes(inner),
        b64url_decode(inner["author_sig"]),
    )
    # GFS-blind: author_sig must NOT leak onto the wire envelope (it's inside
    # the ciphertext, never a plaintext field).
    assert "author_sig" not in envelope
    assert inner["author_sig"] not in blob


async def test_global_space_also_relays(env):
    await env["make_space"]("sp-glob", SpaceType.GLOBAL, with_seed=True)
    await env["bus"].publish(
        SpacePostCreated(post=_post(env["author_user_id"]), space_id="sp-glob")
    )
    assert len(env["gfs"].calls) == 1


async def test_no_seed_no_relay(env):
    await env["make_space"]("sp-noseed", SpaceType.PUBLIC, with_seed=False)
    await env["bus"].publish(
        SpacePostCreated(post=_post(env["author_user_id"]), space_id="sp-noseed")
    )
    assert env["gfs"].calls == []


async def test_private_space_no_relay(env):
    await env["make_space"]("sp-priv", SpaceType.PRIVATE, with_seed=True)
    await env["bus"].publish(
        SpacePostCreated(post=_post(env["author_user_id"]), space_id="sp-priv")
    )
    assert env["gfs"].calls == []


async def test_household_space_no_relay(env):
    await env["make_space"]("sp-hh", SpaceType.HOUSEHOLD, with_seed=True)
    await env["bus"].publish(
        SpacePostCreated(post=_post(env["author_user_id"]), space_id="sp-hh")
    )
    assert env["gfs"].calls == []


async def test_inbound_origin_post_not_relayed_loop_guard(env):
    await env["make_space"]("sp-loop", SpaceType.PUBLIC, with_seed=True)
    await env["bus"].publish(
        SpacePostCreated(
            post=_post(env["author_user_id"]),
            space_id="sp-loop",
            origin_instance_id="beta.home",
        )
    )
    assert env["gfs"].calls == []


def _remote_relay(*, post_id="rpost-1", content="remote authored", space_id="sp-pub"):
    """Build a VALID public_relay inner authored by a DIFFERENT household."""
    author_kp = generate_identity_keypair()
    username = "bob"
    author_user_id = derive_user_id(author_kp.public_key, username)
    post = Post(
        id=post_id,
        author=author_user_id,
        type=PostType.TEXT,
        content=content,
        created_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
    )
    inner = build_signed_author_inner(
        post=post,
        space_id=space_id,
        author_username=username,
        author_pk=author_kp.public_key,
        author_identity_seed=author_kp.private_key,
        origin_instance_id="beta.home",
    )
    return author_kp, author_user_id, inner


async def test_remote_authored_relay_happy_path(env):
    skp = await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    author_kp, author_user_id, relay = _remote_relay()
    await env["bus"].publish(
        SpacePostCreated(
            post=_post(author_user_id),
            space_id="sp-pub",
            origin_instance_id="beta.home",
            public_relay=relay,
        )
    )
    assert len(env["gfs"].calls) == 1
    call = env["gfs"].calls[0]
    assert call["event_type"] == AUTHORITY_EVENT_SPACE_POST_PUBLIC
    assert "from_instance" not in call
    envelope = call["payload"]
    assert set(envelope) == {
        "space_id",
        "epoch",
        "encrypted_payload",
        "authority_sig",
        "authority_sig_suite",
    }
    # GFS-blind: no plaintext content leaks.
    blob = json.dumps(envelope)
    assert "remote authored" not in blob
    # Authority signature is THIS seed-holder's, verifiable against space pubkey.
    assert verify_authority_event(
        event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
        space_id="sp-pub",
        payload=strip_authority_sig_fields(envelope),
        authority_sig=envelope["authority_sig"],
        authority_sig_suite=envelope["authority_sig_suite"],
        space_public_key=skp.public_key,
    )
    # Decrypts to the ORIGINAL inner — author_sig intact, author_pk = original.
    pt = await env["crypto"].decrypt(
        "sp-pub", envelope["epoch"], envelope["encrypted_payload"]
    )
    inner = json.loads(pt)
    assert _unpadded(inner) == relay
    assert inner["author_pk"] == author_kp.public_key.hex()
    assert inner["author_user_id"] == author_user_id
    assert verify_ed25519(
        author_kp.public_key,
        author_signing_bytes(inner),
        b64url_decode(inner["author_sig"]),
    )


async def test_remote_authored_not_seed_holder_not_relayed(env):
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=False)
    _kp, author_user_id, relay = _remote_relay()
    await env["bus"].publish(
        SpacePostCreated(
            post=_post(author_user_id),
            space_id="sp-pub",
            origin_instance_id="beta.home",
            public_relay=relay,
        )
    )
    assert env["gfs"].calls == []


async def test_remote_authored_forged_relay_not_relayed(env):
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    _kp, author_user_id, relay = _remote_relay()
    # Tamper a signed field AFTER signing → author_sig no longer verifies.
    relay["content"] = "tampered after signing"
    await env["bus"].publish(
        SpacePostCreated(
            post=_post(author_user_id),
            space_id="sp-pub",
            origin_instance_id="beta.home",
            public_relay=relay,
        )
    )
    assert env["gfs"].calls == []


async def test_remote_authored_self_cert_mismatch_not_relayed(env):
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    _kp, author_user_id, relay = _remote_relay()
    # author_user_id no longer derives from (author_pk, author_username).
    relay["author_user_id"] = "not-the-derived-id"
    await env["bus"].publish(
        SpacePostCreated(
            post=_post(author_user_id),
            space_id="sp-pub",
            origin_instance_id="beta.home",
            public_relay=relay,
        )
    )
    assert env["gfs"].calls == []


async def test_remote_authored_cross_space_inner_not_relayed(env):
    """A validly-signed inner authored for space X must NOT be relayed under
    space Y's envelope — cross-space injection. The inner's signed
    ``space_id`` ("sp-other") differs from the broadcast space ("sp-pub"),
    so the relay is refused (GFS never called)."""
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    # Inner is validly signed FOR a different space ("sp-other").
    _kp, author_user_id, relay = _remote_relay(space_id="sp-other")
    await env["bus"].publish(
        SpacePostCreated(
            post=_post(author_user_id),
            space_id="sp-pub",
            origin_instance_id="beta.home",
            public_relay=relay,
        )
    )
    assert env["gfs"].calls == []


async def test_remote_authored_private_space_not_relayed(env):
    await env["make_space"]("sp-priv", SpaceType.PRIVATE, with_seed=True)
    _kp, author_user_id, relay = _remote_relay(space_id="sp-priv")
    await env["bus"].publish(
        SpacePostCreated(
            post=_post(author_user_id),
            space_id="sp-priv",
            origin_instance_id="beta.home",
            public_relay=relay,
        )
    )
    assert env["gfs"].calls == []


async def test_inbound_no_public_relay_not_relayed(env):
    """An inbound event with no public_relay is the pure loop guard — never
    re-fan, no crash."""
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    await env["bus"].publish(
        SpacePostCreated(
            post=_post(env["author_user_id"]),
            space_id="sp-pub",
            origin_instance_id="beta.home",
            public_relay=None,
        )
    )
    assert env["gfs"].calls == []


async def test_calendar_event_post_not_relayed(env):
    """A calendar-derived post (linked_event_id set) is derived locally on
    every household — relaying it would double up. Skip."""
    await env["make_space"]("sp-cal", SpaceType.PUBLIC, with_seed=True)
    post = Post(
        id="post-cal",
        author=env["author_user_id"],
        type=PostType.EVENT,
        content="event",
        created_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        linked_event_id="evt-1",
    )
    await env["bus"].publish(SpacePostCreated(post=post, space_id="sp-cal"))
    assert env["gfs"].calls == []


async def test_anchor_authored_post_carries_signed_anchor(env):
    """A local author whose ``user_id`` derives from a uuid ``identity_anchor``
    (not the username) relays an inner that carries the anchor, and the inner
    self-certifies via the anchor (the subscriber-side check accepts it)."""
    db = env["db"]
    # Insert a local user whose user_id derives from a uuid anchor, NOT username.
    anchor = "2f3c9d1e4b5a6789abcdef0123456789"
    anchored_user_id = derive_user_id(env["own_pk"], anchor)
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state, identity_anchor) "
        "VALUES(?, 'carol', 'Carol', 'active', ?)",
        (anchored_user_id, anchor),
    )
    skp = await env["make_space"]("sp-anchor", SpaceType.PUBLIC, with_seed=True)
    await env["bus"].publish(
        SpacePostCreated(post=_post(anchored_user_id), space_id="sp-anchor")
    )
    assert len(env["gfs"].calls) == 1
    envelope = env["gfs"].calls[0]["payload"]
    pt = await env["crypto"].decrypt(
        "sp-anchor", envelope["epoch"], envelope["encrypted_payload"]
    )
    inner = json.loads(pt)
    assert inner["identity_anchor"] == anchor
    assert inner["author_user_id"] == anchored_user_id
    # The relayed inner self-certifies via the anchor — username derivation
    # would NOT match, proving the anchor is the derivation input on the wire.
    assert derive_user_id(env["own_pk"], "carol") != anchored_user_id
    assert verify_signed_author_inner(inner) is True
    _ = skp


async def test_legacy_username_anchored_author_relays_v25_compatible_inner(env):
    """REGRESSION (live cross-version bug): in production EVERY user row has a
    non-NULL ``identity_anchor`` (migration 0041 backfilled ``= username``), so
    the producer always passes an anchor. A legacy, username-anchored author
    MUST still relay an inner with NO ``identity_anchor`` key whose author_sig
    verifies over the 13-field v_25 layout — otherwise a not-yet-upgraded
    subscriber (June 2026 builds, ``OURS=24``) drops every public post."""
    db = env["db"]
    # Mirror the 0041 backfill: anchor == username.
    await db.enqueue(
        "UPDATE users SET identity_anchor = username WHERE user_id = ?",
        (env["author_user_id"],),
    )
    row = await db.fetchone(
        "SELECT identity_anchor FROM users WHERE user_id = ?",
        (env["author_user_id"],),
    )
    assert row[0] == "alice"
    await env["make_space"]("sp-legacy", SpaceType.PUBLIC, with_seed=True)
    await env["bus"].publish(
        SpacePostCreated(post=_post(env["author_user_id"]), space_id="sp-legacy")
    )
    assert len(env["gfs"].calls) == 1
    envelope = env["gfs"].calls[0]["payload"]
    pt = await env["crypto"].decrypt(
        "sp-legacy", envelope["epoch"], envelope["encrypted_payload"]
    )
    inner = json.loads(pt)
    assert "identity_anchor" not in inner
    # A v_25 verifier (13-field layout) accepts the emitted signature...
    assert verify_ed25519(
        env["own_pk"],
        _v25_author_signing_bytes(inner),
        b64url_decode(inner["author_sig"]),
    )
    # ...and so does a v_26+ verifier.
    assert verify_signed_author_inner(inner) is True


async def test_space_service_created_global_space_can_relay(env):
    """Regression (#gfs-space-key): a GLOBAL space created through
    ``SpaceService.create_space`` — the real production path, with nobody ever
    invited cross-household — must already hold a content key, so the producer
    reaches the relay instead of the "no content key … cannot relay" branch.
    """
    from socialhome.domain.space import SpaceType as _SpaceType
    from socialhome.infrastructure.event_bus import EventBus as _EventBus
    from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
    from socialhome.repositories.user_repo import SqliteUserRepo as _UserRepo
    from socialhome.services.space_service import SpaceService

    db = env["db"]
    svc = SpaceService(
        env["space_repo"],
        SqliteSpacePostRepo(db),
        _UserRepo(db),
        _EventBus(),
        own_instance_id=env["own_iid"],
    )
    svc.attach_space_crypto_service(env["crypto"])
    # Readability is the ``allow_subscribers`` opt-in and it defaults OFF, so
    # a freshly created space relays nothing. This regression is about the
    # content key existing at create time, so opt in explicitly.
    space = await svc.create_space(
        owner_username="alice",
        name="World",
        space_type=_SpaceType.GLOBAL,
        features=SpaceFeatures(allow_subscribers=True),
    )

    await env["bus"].publish(
        SpacePostCreated(post=_post(env["author_user_id"]), space_id=space.id)
    )

    assert len(env["gfs"].calls) == 1
    envelope = env["gfs"].calls[0]["payload"]
    assert envelope["space_id"] == space.id
    pt = await env["crypto"].decrypt(
        space.id, envelope["epoch"], envelope["encrypted_payload"]
    )
    assert json.loads(pt)["post_id"] == "post-1"


# ─── allow_subscribers OFF: listed for discovery, never publicly readable ──


async def test_global_space_without_subscribers_post_not_relayed(env):
    """A GLOBAL space whose owner has NOT opted into subscribers is
    discoverable (its metadata is published to the GFS) but NOT publicly
    readable: no post of it is ever relayed to the GFS subscribers."""
    await env["make_space"](
        "sp-inv",
        SpaceType.GLOBAL,
        with_seed=True,
        allow_subscribers=False,
    )
    await env["bus"].publish(
        SpacePostCreated(post=_post(env["author_user_id"]), space_id="sp-inv")
    )
    assert env["gfs"].calls == []


async def test_public_space_without_subscribers_post_not_relayed(env):
    await env["make_space"](
        "sp-inv-pub",
        SpaceType.PUBLIC,
        with_seed=True,
        allow_subscribers=False,
    )
    await env["bus"].publish(
        SpacePostCreated(post=_post(env["author_user_id"]), space_id="sp-inv-pub")
    )
    assert env["gfs"].calls == []


async def test_remote_authored_post_not_relayed_without_subscribers(env):
    """The owner-offline remote-author relay is gated the same way — a
    seed-holder must not launder another member's post into the (nonexistent)
    public stream of a space that takes no followers."""
    await env["make_space"](
        "sp-pub",
        SpaceType.GLOBAL,
        with_seed=True,
        allow_subscribers=False,
    )
    _kp, author_user_id, relay = _remote_relay()
    await env["bus"].publish(
        SpacePostCreated(
            post=_post(author_user_id),
            space_id="sp-pub",
            origin_instance_id="beta.home",
            public_relay=relay,
        )
    )
    assert env["gfs"].calls == []


# ─── the two dials are independent ────────────────────────────────────────


async def test_invite_only_space_with_subscribers_on_still_relays(env):
    """``invite_only`` + subscribers-ON is a legitimate BROADCAST space: only
    invited people post, but anyone may follow. The join mode must not gate
    the relay — that was the old, wrong model."""
    await env["make_space"](
        "sp-broadcast",
        SpaceType.GLOBAL,
        with_seed=True,
        join_mode=JoinMode.INVITE_ONLY,
        allow_subscribers=True,
    )
    await env["bus"].publish(
        SpacePostCreated(post=_post(env["author_user_id"]), space_id="sp-broadcast")
    )
    assert len(env["gfs"].calls) == 1


async def test_request_join_mode_with_subscribers_on_still_relays(env):
    await env["make_space"](
        "sp-req-open",
        SpaceType.GLOBAL,
        with_seed=True,
        join_mode=JoinMode.REQUEST,
        allow_subscribers=True,
    )
    await env["bus"].publish(
        SpacePostCreated(post=_post(env["author_user_id"]), space_id="sp-req-open")
    )
    assert len(env["gfs"].calls) == 1


async def test_open_to_join_space_with_subscribers_off_does_not_relay(env):
    """…and the mirror image: ``open`` says anyone may JOIN, not that anyone
    may READ. With subscribers off the content stream stays dead."""
    await env["make_space"](
        "sp-open-private",
        SpaceType.GLOBAL,
        with_seed=True,
        join_mode=JoinMode.OPEN,
        allow_subscribers=False,
    )
    await env["bus"].publish(
        SpacePostCreated(post=_post(env["author_user_id"]), space_id="sp-open-private")
    )
    assert env["gfs"].calls == []


# ─── v_49: the relayed inner carries the author household's writer cert ──


class _Seats:
    """Remote-member double: ``instance_id`` → list of role strings."""

    def __init__(self, seats: dict[str, list[str]]):
        self.seats = seats

    async def list_for_instance(
        self, space_id, instance_id, *, include_tombstoned=True
    ):
        return [
            SimpleNamespace(role=r, user_id=f"{instance_id}-u{i}")
            for i, r in enumerate(self.seats.get(instance_id, []))
        ]


class _PeerKeys:
    def __init__(self, pks: dict[str, bytes], versions: dict[str, int] | None = None):
        self.pks = pks
        self.versions = versions

    async def peer_identity_public_key(self, iid):
        return self.pks.get(iid)

    async def peer_supports(self, iid, *, min_version):
        if self.versions is None:
            return True
        return self.versions.get(iid, 0) >= min_version


async def _with_certs(env, *, seats=None, pks=None, versions=None):
    certs = SpaceWriterCertService(
        space_repo=env["space_repo"],
        remote_member_repo=_Seats(seats or {}),
        space_key_repo=SqliteSpaceKeyRepo(env["db"]),
        own_instance_id=env["own_iid"],
        own_identity_pk=env["own_pk"],
    )
    certs.attach_federation(_PeerKeys(pks or {}, versions))
    env["sub"].attach_writer_certs(certs)
    return certs, SpaceMember


async def _decrypted(env, space_id):
    envelope = env["gfs"].calls[-1]["payload"]
    pt = await env["crypto"].decrypt(
        space_id, envelope["epoch"], envelope["encrypted_payload"]
    )
    return envelope, json.loads(pt)


async def test_local_author_inner_carries_our_writer_cert(env):
    skp = await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    await env["space_repo"].save_member(
        SpaceMember(
            space_id="sp-pub",
            user_id=env["author_user_id"],
            role="owner",
            joined_at="2026-01-01T00:00:00+00:00",
        )
    )
    await _with_certs(env)
    await env["bus"].publish(
        SpacePostCreated(post=_post(env["author_user_id"]), space_id="sp-pub")
    )
    envelope, inner = await _decrypted(env, "sp-pub")
    # Never on the wire in plaintext.
    assert "writer_cert" not in envelope
    verify_writer_cert(
        WriterCert.from_wire(inner["writer_cert"]),
        space_pubkey=skp.public_key,
        space_id="sp-pub",
        epoch=envelope["epoch"],
        author_pk=env["own_pk"],
        required_scope="write",
    )
    # The author signature still verifies — the cert is outside it.
    assert verify_signed_author_inner(inner)


async def _remote_cert(skp, author_pk, *, epoch, space_id="sp-pub", scope="write"):
    return sign_writer_cert(
        space_seed=skp.private_key,
        space_id=space_id,
        epoch=epoch,
        instance_pk=author_pk,
        scope=scope,
    ).to_wire()


async def test_remote_author_cert_is_restamped_for_the_current_epoch(env):
    skp = await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    author_kp, author_user_id, relay = _remote_relay()
    await _with_certs(
        env, seats={"beta.home": ["member"]}, pks={"beta.home": author_kp.public_key}
    )
    old_epoch = await env["crypto"].get_current_epoch("sp-pub")
    relay["writer_cert"] = await _remote_cert(
        skp, author_kp.public_key, epoch=old_epoch
    )
    await env["crypto"].rotate_epoch("sp-pub")
    await env["bus"].publish(
        SpacePostCreated(
            post=_post(author_user_id),
            space_id="sp-pub",
            origin_instance_id="beta.home",
            public_relay=relay,
        )
    )
    envelope, inner = await _decrypted(env, "sp-pub")
    assert envelope["epoch"] == old_epoch + 1
    verify_writer_cert(
        WriterCert.from_wire(inner["writer_cert"]),
        space_pubkey=skp.public_key,
        space_id="sp-pub",
        epoch=envelope["epoch"],
        author_pk=author_kp.public_key,
        required_scope="write",
    )
    assert verify_signed_author_inner(inner)


@pytest.mark.parametrize(
    "mutate",
    ["forged", "other_household", "comment_scope", "other_space"],
)
async def test_remote_author_with_a_bad_cert_is_not_relayed(env, mutate, caplog):
    skp = await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    author_kp, author_user_id, relay = _remote_relay()
    await _with_certs(
        env, seats={"beta.home": ["member"]}, pks={"beta.home": author_kp.public_key}
    )
    epoch = await env["crypto"].get_current_epoch("sp-pub")
    if mutate == "forged":
        relay["writer_cert"] = await _remote_cert(
            generate_space_keypair(), author_kp.public_key, epoch=epoch
        )
    elif mutate == "other_household":
        relay["writer_cert"] = await _remote_cert(
            skp, generate_identity_keypair().public_key, epoch=epoch
        )
    elif mutate == "comment_scope":
        relay["writer_cert"] = await _remote_cert(
            skp, author_kp.public_key, epoch=epoch, scope="comment"
        )
    else:
        relay["writer_cert"] = await _remote_cert(
            skp, author_kp.public_key, epoch=epoch, space_id="sp-other"
        )
    await env["bus"].publish(
        SpacePostCreated(
            post=_post(author_user_id),
            space_id="sp-pub",
            origin_instance_id="beta.home",
            public_relay=relay,
        )
    )
    assert env["gfs"].calls == []
    assert "writer cert" in caplog.text


async def test_remote_author_whose_seat_is_gone_is_not_relayed(env, caplog):
    """A valid cert from an earlier epoch no longer counts once the
    household holds no writer seat — the host re-stamps only live writers."""
    skp = await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    author_kp, author_user_id, relay = _remote_relay()
    await _with_certs(env, seats={}, pks={"beta.home": author_kp.public_key})
    epoch = await env["crypto"].get_current_epoch("sp-pub")
    relay["writer_cert"] = await _remote_cert(skp, author_kp.public_key, epoch=epoch)
    await env["bus"].publish(
        SpacePostCreated(
            post=_post(author_user_id),
            space_id="sp-pub",
            origin_instance_id="beta.home",
            public_relay=relay,
        )
    )
    assert env["gfs"].calls == []
    assert "writer cert" in caplog.text


async def test_pre_v49_author_without_a_cert_keeps_the_host_path(env):
    """The migration tripwire: no cert from a pre-v49 origin whose pinned
    key is the inner's author key → today's host-signed relay."""
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    author_kp, author_user_id, relay = _remote_relay()
    await _with_certs(
        env, pks={"beta.home": author_kp.public_key}, versions={"beta.home": 48}
    )
    await env["bus"].publish(
        SpacePostCreated(
            post=_post(author_user_id),
            space_id="sp-pub",
            origin_instance_id="beta.home",
            public_relay=relay,
        )
    )
    _envelope, inner = await _decrypted(env, "sp-pub")
    assert _unpadded(inner) == relay


async def test_v49_origin_without_a_cert_is_not_relayed(env, caplog):
    """A v49 household always attaches its cert; a hint without one is a
    stripped cert, not an older author."""
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    author_kp, author_user_id, relay = _remote_relay()
    await _with_certs(
        env, pks={"beta.home": author_kp.public_key}, versions={"beta.home": 49}
    )
    await env["bus"].publish(
        SpacePostCreated(
            post=_post(author_user_id),
            space_id="sp-pub",
            origin_instance_id="beta.home",
            public_relay=relay,
        )
    )
    assert env["gfs"].calls == []
    assert "without a writer cert" in caplog.text


async def test_legacy_hint_naming_another_key_is_not_relayed(env, caplog):
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    _author_kp, author_user_id, relay = _remote_relay()
    await _with_certs(
        env,
        pks={"beta.home": generate_identity_keypair().public_key},
        versions={"beta.home": 48},
    )
    await env["bus"].publish(
        SpacePostCreated(
            post=_post(author_user_id),
            space_id="sp-pub",
            origin_instance_id="beta.home",
            public_relay=relay,
        )
    )
    assert env["gfs"].calls == []
    assert "identity key" in caplog.text


# ─── v_49: no host dedupe — older followers read only the host's copy ───


async def test_a_member_published_post_is_still_relayed_by_the_host(env):
    """The author also publishes the post over the GFS itself, but the host
    keeps relaying it: followers on an older build ignore ``space_item``,
    and receivers that read both dedupe by post id."""
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    _kp, author_user_id, relay = _remote_relay()
    await env["bus"].publish(
        SpacePostCreated(
            post=_post(author_user_id),
            space_id="sp-pub",
            origin_instance_id="beta.home",
            public_relay=relay,
        )
    )
    assert len(env["gfs"].calls) == 1


# ─── Size padding: every host-relay plaintext lands on a bucket ─────────


async def test_relayed_post_plaintext_is_padded_to_a_bucket(env):
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    await env["bus"].publish(
        SpacePostCreated(post=_post(env["author_user_id"]), space_id="sp-pub")
    )
    envelope = env["gfs"].calls[-1]["payload"]
    pt = await env["crypto"].decrypt(
        "sp-pub", envelope["epoch"], envelope["encrypted_payload"]
    )
    assert len(pt) in ITEM_SIZE_BUCKETS
    # The padding sits outside the author signature.
    assert verify_signed_author_inner(json.loads(pt))


async def test_a_member_hint_carrying_an_authority_kind_is_not_relayed(env, caplog):
    """A member's own pre-signed inner must never look like an authority
    notice on the follower side — the seed holder refuses it outright."""
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    _kp, author_user_id, relay = _remote_relay()
    relay[AUTHORITY_KIND_FIELD] = AUTHORITY_KIND_REMOVAL
    await env["bus"].publish(
        SpacePostCreated(
            post=_post(author_user_id),
            space_id="sp-pub",
            origin_instance_id="beta.home",
            public_relay=relay,
        )
    )
    assert env["gfs"].calls == []
    assert "authority" in caplog.text


# ─── Removals: a seed holder relays every host-path delete ──────────────


_ENVELOPE_KEYS = {
    "space_id",
    "epoch",
    "encrypted_payload",
    "authority_sig",
    "authority_sig_suite",
}


async def _removal_inner(env, skp, space_id="sp-pub") -> dict:
    call = env["gfs"].calls[-1]
    assert call["event_type"] == AUTHORITY_EVENT_SPACE_POST_PUBLIC
    envelope = call["payload"]
    assert set(envelope) == _ENVELOPE_KEYS
    assert verify_authority_event(
        event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
        space_id=space_id,
        payload=strip_authority_sig_fields(envelope),
        authority_sig=envelope["authority_sig"],
        authority_sig_suite=envelope["authority_sig_suite"],
        space_public_key=skp.public_key,
    )
    pt = await env["crypto"].decrypt(
        space_id, envelope["epoch"], envelope["encrypted_payload"]
    )
    # Padded like every other relay item: a removal looks like a short post.
    assert len(pt) in ITEM_SIZE_BUCKETS
    return json.loads(pt)


async def test_a_moderator_post_removal_is_relayed_without_who_removed_it(env):
    skp = await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    await env["bus"].publish(
        PostDeleted(
            post_id="post-9",
            space_id="sp-pub",
            actor_user_id="moderator-user",
            author_user_id="author-user",
        )
    )
    assert len(env["gfs"].calls) == 1
    blob = json.dumps(env["gfs"].calls[0])
    # The GFS sees no item id, author or moderator.
    for secret in ("post-9", "moderator-user", "author-user"):
        assert secret not in blob
    inner = await _removal_inner(env, skp)
    assert _unpadded(inner) == {
        AUTHORITY_KIND_FIELD: AUTHORITY_KIND_REMOVAL,
        "space_id": "sp-pub",
        "target": "post",
        "item_id": "post-9",
        "post_id": "post-9",
        # Whose it was — for the follower's tombstone rule; inside only.
        "author_user_id": "author-user",
    }
    # Followers learn nothing about who removed it.
    assert "moderator-user" not in json.dumps(inner)


async def test_a_comment_removal_is_relayed(env):
    skp = await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
    await env["bus"].publish(
        CommentDeleted(
            post_id="post-1",
            comment_id="c-1",
            space_id="sp-pub",
            actor_user_id="mod",
            author_user_id="someone",
        )
    )
    inner = await _removal_inner(env, skp)
    assert _unpadded(inner) == {
        AUTHORITY_KIND_FIELD: AUTHORITY_KIND_REMOVAL,
        "space_id": "sp-pub",
        "target": "comment",
        "item_id": "c-1",
        "post_id": "post-1",
        "author_user_id": "someone",
    }


async def test_a_federated_delete_applied_here_is_relayed_too(env):
    """A moderator on another household removes the post; the host applies
    the federated delete and relays the removal on its authority."""
    skp = await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    await env["bus"].publish(
        PostDeleted(post_id="post-2", space_id="sp-pub", origin_instance_id="m.home")
    )
    inner = await _removal_inner(env, skp)
    assert inner["item_id"] == "post-2"


@pytest.mark.parametrize("case", ["no_seed", "private", "no_subscribers", "feed"])
async def test_removal_is_not_relayed_where_nothing_was(env, case):
    if case == "no_seed":
        await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=False)
    elif case == "private":
        await env["make_space"]("sp-pub", SpaceType.PRIVATE, with_seed=True)
    elif case == "no_subscribers":
        await env["make_space"](
            "sp-pub", SpaceType.PUBLIC, with_seed=True, allow_subscribers=False
        )
    else:
        await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    await env["bus"].publish(
        PostDeleted(post_id="post-3", space_id=None if case == "feed" else "sp-pub")
    )
    await env["bus"].publish(
        CommentDeleted(
            post_id="post-3",
            comment_id="c-3",
            space_id=None if case == "feed" else "sp-pub",
        )
    )
    assert env["gfs"].calls == []


async def test_a_calendar_post_removal_is_not_relayed(env):
    """Calendar-derived posts are never relayed, so neither is their removal."""
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)

    class _Posts:
        async def get(self, post_id):
            post = _post("x")
            return "sp-pub", Post(
                id=post_id,
                author=post.author,
                type=PostType.TEXT,
                content=None,
                created_at=post.created_at,
                linked_event_id="ev-1",
            )

    env["sub"].attach_posts(_Posts())
    await env["bus"].publish(PostDeleted(post_id="post-cal", space_id="sp-pub"))
    assert env["gfs"].calls == []


async def test_a_removal_with_an_unusable_id_is_not_relayed(env):
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    await env["bus"].publish(PostDeleted(post_id="x" * 500, space_id="sp-pub"))
    assert env["gfs"].calls == []


async def test_a_removal_without_a_content_key_is_dropped(env, caplog):
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)

    async def _no_key(*_a, **_k):
        raise RuntimeError("no key")

    with patch.object(SpaceContentEncryption, "encrypt", _no_key):
        await env["bus"].publish(PostDeleted(post_id="post-4", space_id="sp-pub"))
    assert env["gfs"].calls == []
    assert "no content key" in caplog.text


# ─── Approved moderated posts: relayed under the author's signature ─────


async def _approved_env(env, *, seats=("member",)):
    """A remote author household whose instance id is its key fingerprint,
    a MODERATED public space, and the post the host just published from the
    queue with the submitter's signed copy."""
    skp = await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    space = await env["space_repo"].get("sp-pub")
    await env["space_repo"].save(_with_posts_level(space, "moderated"))
    author_kp = generate_identity_keypair()
    author_iid = derive_instance_id(author_kp.public_key)
    author_user_id = derive_user_id(author_kp.public_key, "bob")
    await _with_certs(
        env,
        seats={author_iid: list(seats)},
        pks={author_iid: author_kp.public_key},
    )
    post = Post(
        id=mint_owner_bound_id(
            SPACE_POST_KIND, space_id="sp-pub", owner_user_id=author_user_id
        ),
        author=author_user_id,
        type=PostType.TEXT,
        content="reviewed and released",
        created_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
    )
    # Signed by the submitter at submission time (created_at = submitted).
    relay = build_signed_author_inner(
        post=post,
        space_id="sp-pub",
        author_username="bob",
        author_pk=author_kp.public_key,
        author_identity_seed=author_kp.private_key,
        origin_instance_id=author_iid,
    )
    return skp, author_kp, author_iid, post, relay


def _with_posts_level(space, level):
    return dataclasses.replace(
        space,
        features=dataclasses.replace(
            space.features, posts_access=SpaceFeatureAccess(level)
        ),
    )


async def test_an_approved_remote_post_is_relayed_under_its_authors_signature(env):
    skp, author_kp, author_iid, post, relay = await _approved_env(env)
    await env["bus"].publish(
        SpacePostCreated(
            post=post,
            space_id="sp-pub",
            approved_by="the-moderator",
            public_relay=relay,
        )
    )
    assert len(env["gfs"].calls) == 1
    blob = json.dumps(env["gfs"].calls[0])
    for secret in (post.id, post.author, "the-moderator", "reviewed and released"):
        assert secret not in blob
    inner = await _removal_inner(env, skp)
    assert is_approved(inner)
    # The author's own signature still proves authorship.
    assert verify_signed_author_inner(inner)
    assert inner["author_sig"] == relay["author_sig"]
    assert inner["post_id"] == post.id
    assert inner["origin_instance_id"] == author_iid
    assert "the-moderator" not in json.dumps(inner)
    envelope = env["gfs"].calls[0]["payload"]
    verify_writer_cert(
        WriterCert.from_wire(inner["writer_cert"]),
        space_pubkey=skp.public_key,
        space_id="sp-pub",
        epoch=envelope["epoch"],
        author_pk=author_kp.public_key,
        required_scope="comment",
    )


async def test_an_approved_post_without_a_signed_copy_is_not_relayed(env, caplog):
    """An older submitter attached none: a seed holder never vouches for
    authorship on its own."""
    caplog.set_level(logging.INFO)
    _skp, _kp, _iid, post, _relay = await _approved_env(env)
    await env["bus"].publish(
        SpacePostCreated(post=post, space_id="sp-pub", approved_by="mod")
    )
    assert env["gfs"].calls == []
    assert "no valid author-signed copy" in caplog.text


async def test_a_remote_post_without_an_approval_is_not_relayed(env):
    _skp, _kp, _iid, post, relay = await _approved_env(env)
    await env["bus"].publish(
        SpacePostCreated(post=post, space_id="sp-pub", public_relay=relay)
    )
    assert env["gfs"].calls == []


async def test_a_signed_copy_of_other_words_is_not_relayed(env):
    """Followers see exactly what the reviewers approved, or nothing."""
    _skp, _kp, _iid, post, relay = await _approved_env(env)
    await env["bus"].publish(
        SpacePostCreated(
            post=dataclasses.replace(post, content="what the moderators saw"),
            space_id="sp-pub",
            approved_by="mod",
            public_relay=relay,
        )
    )
    assert env["gfs"].calls == []


async def test_an_approved_post_of_a_household_without_a_seat_is_not_relayed(
    env, caplog
):
    _skp, _kp, _iid, post, relay = await _approved_env(env, seats=())
    await env["bus"].publish(
        SpacePostCreated(
            post=post, space_id="sp-pub", approved_by="mod", public_relay=relay
        )
    )
    assert env["gfs"].calls == []
    assert "writer" in caplog.text


async def test_an_approved_post_without_a_content_key_is_dropped(env, caplog):
    _skp, _kp, _iid, post, relay = await _approved_env(env)

    async def _no_key(*_a, **_k):
        return None

    with patch.object(SpaceContentEncryption, "get_current_epoch", _no_key):
        await env["bus"].publish(
            SpacePostCreated(
                post=post, space_id="sp-pub", approved_by="mod", public_relay=relay
            )
        )
    assert env["gfs"].calls == []
    assert "no content key" in caplog.text


async def test_a_failed_gfs_publish_is_logged_not_raised(env, caplog):
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)

    async def _boom(**_kw):
        raise RuntimeError("gfs down")

    env["gfs"].publish_space_event = _boom
    await env["bus"].publish(PostDeleted(post_id="post-5", space_id="sp-pub"))
    assert "post removal relay failed" in caplog.text


async def _posts_env(env):
    repo = SqliteSpacePostRepo(env["db"])
    env["sub"].attach_posts(repo)
    return repo


async def test_a_federated_delete_carries_the_author_from_the_held_row(env):
    skp = await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    posts = await _posts_env(env)
    await posts.save("sp-pub", _post("row-author"))
    await env["bus"].publish(
        PostDeleted(post_id="post-1", space_id="sp-pub", origin_instance_id="m.home")
    )
    inner = await _removal_inner(env, skp)
    assert inner["author_user_id"] == "row-author"


async def test_a_removal_of_an_item_never_held_here_is_not_relayed(env):
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    await _posts_env(env)
    await env["bus"].publish(PostDeleted(post_id="ghost", space_id="sp-pub"))
    await env["bus"].publish(
        CommentDeleted(post_id="ghost", comment_id="c-ghost", space_id="sp-pub")
    )
    assert env["gfs"].calls == []


async def test_a_comment_removal_carries_the_author_from_the_held_row(env):
    skp = await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    posts = await _posts_env(env)
    await posts.save("sp-pub", _post("post-author"))
    await posts.add_comment(
        Comment(
            id="c-held",
            post_id="post-1",
            author="comment-author",
            type=CommentType.TEXT,
            content="hi",
            created_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        ),
        space_id="sp-pub",
    )
    await env["bus"].publish(
        CommentDeleted(post_id="post-1", comment_id="c-held", space_id="sp-pub")
    )
    inner = await _removal_inner(env, skp)
    assert inner["author_user_id"] == "comment-author"


async def test_a_removal_in_an_archived_space_is_not_relayed(env):
    await env["make_space"]("sp-pub", SpaceType.PUBLIC, with_seed=True)
    space = await env["space_repo"].get("sp-pub")
    await env["space_repo"].save(dataclasses.replace(space, archived=True))
    await env["bus"].publish(PostDeleted(post_id="post-1", space_id="sp-pub"))
    assert env["gfs"].calls == []
