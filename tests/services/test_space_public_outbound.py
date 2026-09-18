"""Tests for :class:`SpacePublicOutbound` (Phase 5a2 producer).

A seed-holding household relays a PUBLIC/GLOBAL space post to the GFS as
an *encrypted, authority-signed* envelope — the GFS (and any relay) sees
only ``{space_id, epoch, encrypted_payload, authority_sig, ...}``, never
the post content or author. Households without the seed, non-public
spaces, and inbound-driven (loop) events do not relay.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

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
from socialhome.domain.events import SpacePostCreated
from socialhome.domain.post import Post, PostType
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_key_repo import SqliteSpaceKeyRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_crypto_service import SpaceContentEncryption
from socialhome.services.space_public_outbound import SpacePublicOutbound


class _CaptureGfs:
    """Stub for gfs_connection_service.publish_space_event — records calls."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def publish_space_event(
        self, *, space_id, event_type, payload, from_instance
    ) -> int:
        self.calls.append(
            {
                "space_id": space_id,
                "event_type": event_type,
                "payload": payload,
                "from_instance": from_instance,
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

    async def _make_space(space_id: str, stype: SpaceType, *, with_seed: bool):
        skp = generate_space_keypair()
        await space_repo.save(
            Space(
                id=space_id,
                name="S",
                owner_instance_id=own_iid,
                owner_username="alice",
                identity_public_key=skp.public_key.hex(),
                config_sequence=0,
                features=SpaceFeatures(),
                space_type=stype,
                join_mode=JoinMode.OPEN,
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
    assert call["from_instance"] == env["own_iid"]
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
    assert call["from_instance"] == env["own_iid"]
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
    assert inner == relay
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
    space = await svc.create_space(
        owner_username="alice", name="World", space_type=_SpaceType.GLOBAL
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
