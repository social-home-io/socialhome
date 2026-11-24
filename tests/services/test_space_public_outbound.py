"""Tests for :class:`SpacePublicOutbound` — the host's authority items in a
global space's opaque channel (v_96).

The space's OWNER (seed holder) puts into the channel what only the seed can
sign: a remote member's post its author did not put there itself, an
approved post from the moderation queue, and removals — each as a channel
authority item (the inner signed by the space authority, binding the epoch,
sealed under the content key). A space with no channel here gets nothing:
the space-keyed ``/gfs/publish`` relay is gone, and our own authors' posts
go into the channel through the member publisher.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from socialhome.crypto import (
    b64url_decode,
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
    generate_space_keypair,
    verify_ed25519,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import CommentDeleted, PostDeleted, SpacePostCreated
from socialhome.domain.post import Comment, CommentType, Post, PostType
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatureAccess,
    SpaceFeatures,
    SpaceMember,
    SpaceType,
)
from socialhome.domain.space_item import (
    AUTHORITY_KIND_FIELD,
    AUTHORITY_KIND_REMOVAL,
    CHANNEL_SENT_FIELD,
    ITEM_SIZE_BUCKETS,
    PAD_FIELD,
)
from socialhome.domain.writer_cert import WriterCert
from socialhome.federation.owner_bound_id import SPACE_POST_KIND, mint_owner_bound_id
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_key_repo import SqliteSpaceKeyRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services import space_public_outbound
from socialhome.services.space_crypto_service import SpaceContentEncryption
from socialhome.services.space_public_author import (
    author_signing_bytes,
    build_signed_author_inner,
    verify_signed_author_inner,
)
from socialhome.services.space_public_authority import (
    is_approved,
    open_channel_authority,
)
from socialhome.services.space_public_outbound import SpacePublicOutbound
from socialhome.services.space_writer_cert_service import SpaceWriterCertService
from socialhome.writer_cert import sign_writer_cert, verify_writer_cert


def _unpadded(inner: dict) -> dict:
    """The decrypted inner without its size padding (``_pad``)."""
    return {k: v for k, v in inner.items() if k != PAD_FIELD}


class _CaptureChannel:
    """The channel service as the outbound sees it: which spaces run a
    channel here (``spaces``), and what went into it (``calls``)."""

    def __init__(self) -> None:
        self.spaces: set[str] = set()
        self.calls: list[dict] = []
        self.fail = False

    async def has_channel(self, space_id):
        return space_id in self.spaces

    async def publish_current(self, space_id, epoch, ciphertext):
        if self.fail:
            raise RuntimeError("gfs down")
        self.calls.append({"space_id": space_id, "epoch": epoch, "ct": ciphertext})
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
    gfs = _CaptureChannel()
    skps: dict = {}

    async def _make_space(
        space_id: str,
        stype: SpaceType,
        *,
        with_seed: bool,
        join_mode: JoinMode = JoinMode.OPEN,
        allow_subscribers: bool = True,
        owner: str = own_iid,
        channel: bool = True,
    ):
        skp = generate_space_keypair()
        await space_repo.save(
            Space(
                id=space_id,
                name="S",
                owner_instance_id=owner,
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
        skps[space_id] = skp
        if channel and stype is SpaceType.GLOBAL:
            gfs.spaces.add(space_id)
        return skp

    sub = SpacePublicOutbound(
        bus=bus,
        space_repo=space_repo,
        space_crypto=crypto,
        user_repo=user_repo,
    )
    sub.attach_identity(own_instance_id=own_iid)
    sub.attach_channels(gfs)
    sub.wire()
    yield {
        "db": db,
        "bus": bus,
        "gfs": gfs,
        "skps": skps,
        "crypto": crypto,
        "space_repo": space_repo,
        "make_space": _make_space,
        "author_user_id": author_user_id,
        "own_iid": own_iid,
        "own_pk": own_kp.public_key,
        "own_seed": own_kp.private_key,
        "sub": sub,
    }
    await db.shutdown()


def _post(author: str) -> Post:
    return Post(
        id="post-1",
        author=author,
        type=PostType.TEXT,
        content="hello public space",
        created_at=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
    )


async def test_no_seed_no_relay(env):
    await env["make_space"]("sp-noseed", SpaceType.GLOBAL, with_seed=False)
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
    await env["make_space"]("sp-loop", SpaceType.GLOBAL, with_seed=True)
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
        created_at=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
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
    await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
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
    # Into the channel as ciphertext: no plaintext content leaks.
    assert "remote authored" not in json.dumps(env["gfs"].calls[0])
    # The inner carries THIS seed holder's authority signature (verified by
    # ``_decrypted``) and is the ORIGINAL inner — author_sig intact.
    _frame, inner = await _decrypted(env, "sp-pub")
    assert _unpadded(inner) == relay
    assert inner["author_pk"] == author_kp.public_key.hex()
    assert inner["author_user_id"] == author_user_id
    assert verify_ed25519(
        author_kp.public_key,
        author_signing_bytes(inner),
        b64url_decode(inner["author_sig"]),
    )


class _Coverage:
    def __init__(self) -> None:
        self.asked: list[tuple[str, str | None]] = []

    async def redundant_on(self, space_id, author_instance_id):
        self.asked.append((space_id, author_instance_id))

        async def _skip(conn) -> bool:
            return True

        return _skip


async def test_remote_authored_not_seed_holder_not_relayed(env):
    await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=False)
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
    await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
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
    await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
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
    await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
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
    await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
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
    await env["make_space"]("sp-cal", SpaceType.GLOBAL, with_seed=True)
    post = Post(
        id="post-cal",
        author=env["author_user_id"],
        type=PostType.EVENT,
        content="event",
        created_at=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
        linked_event_id="evt-1",
    )
    await env["bus"].publish(SpacePostCreated(post=post, space_id="sp-cal"))
    assert env["gfs"].calls == []


# ─── allow_subscribers OFF: listed for discovery, never publicly readable ──


async def test_a_remote_post_goes_into_the_channel_with_subscribers_off(env):
    """v_96 — the channel carries the members' content whatever
    ``allow_subscribers`` says (followers off means no follower seats)."""
    await env["make_space"](
        "sp-pub", SpaceType.GLOBAL, with_seed=True, allow_subscribers=False
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
    assert len(env["gfs"].calls) == 1


@pytest.mark.security
async def test_a_space_without_a_channel_gets_nothing_at_all(env):
    """v_96 — no channel here, no GFS path: no remote-author copy, no
    approved post, no removal (the space-keyed relay is gone; members are
    reached by federation and catch up from the host)."""
    await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True, channel=False)
    _kp, author_user_id, relay = _remote_relay()
    await env["bus"].publish(
        SpacePostCreated(
            post=_post(author_user_id),
            space_id="sp-pub",
            origin_instance_id="beta.home",
            public_relay=relay,
        )
    )
    await env["bus"].publish(PostDeleted(post_id="post-7", space_id="sp-pub"))
    await env["bus"].publish(
        CommentDeleted(post_id="post-7", comment_id="c-7", space_id="sp-pub")
    )
    assert env["gfs"].calls == []


async def test_our_own_authors_post_gets_no_authority_copy(env):
    """Our own author's post goes into the channel through the member
    publisher — never as an authority item here."""
    await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
    await env["bus"].publish(
        SpacePostCreated(post=_post(env["author_user_id"]), space_id="sp-pub")
    )
    assert env["gfs"].calls == []


# ─── the two dials are independent ────────────────────────────────────────


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

    async def space_member_supports(self, iid, *, min_version):
        return await self.peer_supports(iid, min_version=min_version)


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
    """The last channel item: ``(frame, authority inner)`` — the inner's
    space-authority signature verified against the pin, padding checked."""
    frame = env["gfs"].calls[-1]
    opened = await env["crypto"].decrypt(space_id, frame["epoch"], frame["ct"])
    assert len(opened) in ITEM_SIZE_BUCKETS
    inner = open_channel_authority(
        opened,
        space_id=space_id,
        epoch=frame["epoch"],
        space_pubkey_hex=env["skps"][space_id].public_key.hex(),
    )
    assert inner is not None
    return frame, inner


async def _remote_cert(skp, author_pk, *, epoch, space_id="sp-pub", scope="write"):
    return sign_writer_cert(
        space_seed=skp.private_key,
        space_id=space_id,
        epoch=epoch,
        instance_pk=author_pk,
        scope=scope,
    ).to_wire()


async def test_remote_author_cert_is_restamped_for_the_current_epoch(env):
    skp = await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
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
    skp = await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
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
    skp = await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
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
    await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
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
    await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
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
    await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
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
    await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
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


async def test_a_member_hint_carrying_an_authority_kind_is_not_relayed(env, caplog):
    """A member's own pre-signed inner must never look like an authority
    notice on the follower side — the seed holder refuses it outright."""
    await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
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
    _frame, inner = await _decrypted(env, space_id)
    return inner


async def test_a_moderator_post_removal_is_relayed_without_who_removed_it(env):
    skp = await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
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
    skp = await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
    await env["bus"].publish(
        PostDeleted(post_id="post-2", space_id="sp-pub", origin_instance_id="m.home")
    )
    inner = await _removal_inner(env, skp)
    assert inner["item_id"] == "post-2"


@pytest.mark.parametrize("case", ["no_seed", "private", "no_channel", "feed"])
async def test_removal_is_not_relayed_where_nothing_was(env, case):
    if case == "no_seed":
        await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=False)
    elif case == "private":
        await env["make_space"]("sp-pub", SpaceType.PRIVATE, with_seed=True)
    elif case == "no_channel":
        await env["make_space"](
            "sp-pub", SpaceType.GLOBAL, with_seed=True, channel=False
        )
    else:
        await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
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
    await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)

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
    await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
    await env["bus"].publish(PostDeleted(post_id="x" * 500, space_id="sp-pub"))
    assert env["gfs"].calls == []


async def test_a_removal_without_a_content_key_is_dropped(env, caplog):
    await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)

    async def _no_key(*_a, **_k):
        raise RuntimeError("no key")

    with patch.object(SpaceContentEncryption, "encrypt", _no_key):
        await env["bus"].publish(PostDeleted(post_id="post-4", space_id="sp-pub"))
    assert env["gfs"].calls == []
    assert "no content key" in caplog.text


# ─── Approved moderated posts: relayed under the author's signature ─────


async def _approved_env(env, *, seats=("member",), owner=None, stype=SpaceType.GLOBAL):
    """A remote author household whose instance id is its key fingerprint,
    a MODERATED public space, and the post the host just published from the
    queue with the submitter's signed copy."""
    skp = await env["make_space"](
        "sp-pub", stype, with_seed=True, owner=owner or env["own_iid"]
    )
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
        created_at=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
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
    envelope = env["gfs"].calls[0]
    verify_writer_cert(
        WriterCert.from_wire(inner["writer_cert"]),
        space_pubkey=skp.public_key,
        space_id="sp-pub",
        epoch=envelope["epoch"],
        author_pk=author_kp.public_key,
        required_scope="comment",
    )


async def test_an_approved_post_of_a_mixed_household_carries_a_plain_cert(env):
    """I3 — members equal on any path: a household with a plain member next
    to a follower holds ONE reader cert (its comment set binds the
    follower), which no receiver accepts on a post. The approved re-stamp is
    issued without follower seats: a plain cert binding only the member."""
    skp, author_kp, _iid, post, relay = await _approved_env(
        env, seats=("subscriber", "member")
    )
    space = await env["space_repo"].get("sp-pub")
    space = dataclasses.replace(
        space,
        features=dataclasses.replace(space.features, allow_subscriber_react=True),
    )
    await env["space_repo"].save(space)
    await env["bus"].publish(
        SpacePostCreated(
            post=post, space_id="sp-pub", approved_by="mod", public_relay=relay
        )
    )
    assert len(env["gfs"].calls) == 1
    inner = await _removal_inner(env, skp)
    cert = WriterCert.from_wire(inner["writer_cert"])
    assert cert.reader is False
    assert cert.writer_user_ids == (f"{_iid}-u1",)
    assert SpaceWriterCertService.check_item_cert(
        await env["space_repo"].get("sp-pub"),
        inner["writer_cert"],
        epoch=env["gfs"].calls[0]["epoch"],
        author_pk=author_kp.public_key,
        required_scope="comment",
        item_type="post",
    )


async def test_a_reader_cert_is_never_stamped_on_a_post(env):
    """Hardening: whatever path asks, ``_encrypt_with_cert`` refuses to put
    a follower's reader cert on an item type it may not carry."""
    _skp, author_kp, iid, _post, relay = await _approved_env(env, seats=("subscriber",))
    space = await env["space_repo"].get("sp-pub")
    await env["space_repo"].save(
        dataclasses.replace(
            space,
            features=dataclasses.replace(
                space.features,
                allow_subscriber_comment=True,
                allow_subscriber_react=True,
            ),
        )
    )
    sub = env["sub"]
    with pytest.raises(space_public_outbound._NoWriterCert):
        await sub._encrypt_with_cert(
            "sp-pub",
            relay,
            iid,
            expect_pk=author_kp.public_key.hex(),
            required=True,
            required_scope="comment",
        )
    # The same cert on a comment is fine.
    sealed = await sub._encrypt_with_cert(
        "sp-pub",
        relay,
        iid,
        expect_pk=author_kp.public_key.hex(),
        required=True,
        required_scope="comment",
        item_type="comment",
    )
    assert sealed.ct


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
    await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)

    env["gfs"].fail = True
    await env["bus"].publish(PostDeleted(post_id="post-5", space_id="sp-pub"))
    assert "post removal channel publish failed" in caplog.text


async def _posts_env(env):
    repo = SqliteSpacePostRepo(env["db"])
    env["sub"].attach_posts(repo)
    return repo


async def test_a_federated_delete_carries_the_author_from_the_held_row(env):
    skp = await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
    posts = await _posts_env(env)
    await posts.save("sp-pub", _post("row-author"))
    await env["bus"].publish(
        PostDeleted(post_id="post-1", space_id="sp-pub", origin_instance_id="m.home")
    )
    inner = await _removal_inner(env, skp)
    assert inner["author_user_id"] == "row-author"


async def test_a_removal_of_an_item_never_held_here_is_not_relayed(env):
    await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
    await _posts_env(env)
    await env["bus"].publish(PostDeleted(post_id="ghost", space_id="sp-pub"))
    await env["bus"].publish(
        CommentDeleted(post_id="ghost", comment_id="c-ghost", space_id="sp-pub")
    )
    assert env["gfs"].calls == []


async def test_a_comment_removal_carries_the_author_from_the_held_row(env):
    skp = await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
    posts = await _posts_env(env)
    await posts.save("sp-pub", _post("post-author"))
    await posts.add_comment(
        Comment(
            id="c-held",
            post_id="post-1",
            author="comment-author",
            type=CommentType.TEXT,
            content="hi",
            created_at=datetime(2026, 6, 10, tzinfo=UTC),
        ),
        space_id="sp-pub",
    )
    await env["bus"].publish(
        CommentDeleted(post_id="post-1", comment_id="c-held", space_id="sp-pub")
    )
    inner = await _removal_inner(env, skp)
    assert inner["author_user_id"] == "comment-author"


async def test_a_removal_in_an_archived_space_is_not_relayed(env):
    await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
    space = await env["space_repo"].get("sp-pub")
    await env["space_repo"].save(dataclasses.replace(space, archived=True))
    await env["bus"].publish(PostDeleted(post_id="post-1", space_id="sp-pub"))
    assert env["gfs"].calls == []


# ── v_80 — the relayed post's pictures for followers ────────────────────


class _MediaPlan:
    def wire(self):
        return {"a.webp": {"blob_id": "c" * 64, "suite": "s", "epoch": 0, "size": 9}}


class _MediaGfs:
    def __init__(self):
        self.planned = []
        self.items = []
        self.uploaded = []

    async def plan(self, space_id, urls, *, item_id, followers):
        self.planned.append((space_id, list(urls), followers))
        self.items.append(item_id)
        return _MediaPlan()

    async def is_relay_reader(self, iid):  # pragma: no cover - unused here
        return False

    def upload(self, plan):
        self.uploaded.append(plan)


# ─── A delegated admin: only what the seed alone can say ───────────────────

#: The space's owner household, when this one merely holds its seed.
_OWNER_ELSEWHERE = "owner.home"


async def test_a_delegated_admins_own_post_is_not_relayed_under_the_authority(env):
    """It goes out as its household's anonymous item, like any writer's
    (``GfsMemberPublishService``); an authority-signed copy from a household
    that is not the owner would mark it as a seed holder."""
    await env["make_space"](
        "sp-pub", SpaceType.GLOBAL, with_seed=True, owner=_OWNER_ELSEWHERE
    )
    await env["bus"].publish(
        SpacePostCreated(post=_post(env["author_user_id"]), space_id="sp-pub")
    )
    assert env["gfs"].calls == []


async def test_a_delegated_admin_does_not_relay_other_members_posts(env):
    """The owner relays them (and their v_90 authors publish them
    themselves): a delegated admin answering every member post with an
    authority-signed relay would stand out from every other writer."""
    skp = await env["make_space"](
        "sp-pub", SpaceType.GLOBAL, with_seed=True, owner=_OWNER_ELSEWHERE
    )
    author_kp, author_user_id, relay = _remote_relay()
    await _with_certs(
        env, seats={"beta.home": ["member"]}, pks={"beta.home": author_kp.public_key}
    )
    relay["writer_cert"] = await _remote_cert(
        skp,
        author_kp.public_key,
        epoch=await env["crypto"].get_current_epoch("sp-pub"),
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


async def test_a_delegated_admin_still_relays_a_removal(env):
    """Acting for an offline owner: only the seed can tell followers an
    item is gone, so this still goes out (identity-free, like the owner's)."""
    skp = await env["make_space"](
        "sp-pub", SpaceType.GLOBAL, with_seed=True, owner=_OWNER_ELSEWHERE
    )
    await env["bus"].publish(
        PostDeleted(
            post_id="post-9",
            space_id="sp-pub",
            actor_user_id="moderator-user",
            author_user_id="author-user",
        )
    )
    assert len(env["gfs"].calls) == 1
    assert "from_instance" not in env["gfs"].calls[0]
    inner = await _removal_inner(env, skp)
    assert inner[AUTHORITY_KIND_FIELD] == AUTHORITY_KIND_REMOVAL


@pytest.mark.security
@pytest.mark.parametrize("origin", ["m.home", _OWNER_ELSEWHERE])
async def test_a_delegated_admin_answers_no_removal_it_only_received(env, origin):
    """A delete that reached it from another household — over federation,
    by sync or through its fan-out seat on the GFS — gets no authority-signed
    answer. Otherwise a GFS could hand one household's seat a delete and
    watch for the removal notice only a seed holder sends."""
    await env["make_space"](
        "sp-pub", SpaceType.GLOBAL, with_seed=True, owner=_OWNER_ELSEWHERE
    )
    await env["bus"].publish(
        PostDeleted(
            post_id="post-9",
            space_id="sp-pub",
            origin_instance_id=origin,
            author_user_id="author-user",
        )
    )
    await env["bus"].publish(
        CommentDeleted(
            post_id="post-9",
            comment_id="c-9",
            space_id="sp-pub",
            origin_instance_id=origin,
            author_user_id="author-user",
        )
    )
    assert env["gfs"].calls == []


async def test_the_owner_without_an_identity_still_warns(env, caplog):
    """The owner gate must not swallow the wiring warning."""
    await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
    env["sub"].attach_identity(own_instance_id="")
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
    assert "identity not attached" in caplog.text


@pytest.mark.security
async def test_a_delegated_admin_relays_no_post_even_one_marked_released(env):
    """Only the owner releases a queued post, so a release never happens
    here; should a ``SpacePostCreated`` ever arrive marked approved, it must
    still not open an authority-signed relay on a household that merely
    holds the seed."""
    _skp, _author_kp, _iid, post, relay = await _approved_env(
        env, owner=_OWNER_ELSEWHERE
    )
    await env["bus"].publish(
        SpacePostCreated(
            post=post,
            space_id="sp-pub",
            approved_by="the-moderator",
            public_relay=relay,
        )
    )
    await env["bus"].publish(
        SpacePostCreated(
            post=_post(env["author_user_id"]),
            space_id="sp-pub",
            approved_by="the-moderator",
        )
    )
    assert env["gfs"].calls == []


async def test_the_owner_still_relays_a_removal_that_reached_it(env):
    skp = await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
    await env["bus"].publish(
        CommentDeleted(
            post_id="post-1",
            comment_id="c-1",
            space_id="sp-pub",
            origin_instance_id="m.home",
            author_user_id="someone",
        )
    )
    assert (await _removal_inner(env, skp))["item_id"] == "c-1"


async def test_a_removal_without_an_identity_warns(env, caplog):
    await env["make_space"]("sp-pub", SpaceType.GLOBAL, with_seed=True)
    env["sub"].attach_identity(own_instance_id="")
    await env["bus"].publish(
        PostDeleted(post_id="post-9", space_id="sp-pub", origin_instance_id="m.home")
    )
    assert env["gfs"].calls == []
    assert "identity not attached" in caplog.text


# ── v_96: a global space with a channel ─────────────────────────────────


class _Channels:
    """The channel service as the outbound sees it: which spaces run a
    channel here, and what went into it."""

    def __init__(self, spaces: set[str]) -> None:
        self.spaces = spaces
        self.published: list[tuple[str, int, str]] = []

    async def has_channel(self, space_id):
        return space_id in self.spaces

    async def publish_current(self, space_id, epoch, ciphertext):
        self.published.append((space_id, epoch, ciphertext))
        return 1


async def _channel_env(env, *, allow_subscribers=True):
    skp = await env["make_space"](
        "sp-pub",
        SpaceType.GLOBAL,
        with_seed=True,
        allow_subscribers=allow_subscribers,
    )
    channels = _Channels({"sp-pub"})
    env["sub"].attach_channels(channels)
    return skp, channels


async def _channel_inner(env, skp, channels) -> dict:
    space_id, epoch, ct = channels.published[-1]
    opened = await env["crypto"].decrypt(space_id, epoch, ct)
    assert len(opened) in ITEM_SIZE_BUCKETS
    inner = open_channel_authority(
        opened, space_id=space_id, epoch=epoch, space_pubkey_hex=skp.public_key.hex()
    )
    assert inner is not None
    return inner


async def test_the_owners_own_post_gets_no_authority_copy_on_a_channel(env):
    """Q4: the owner's own post goes into its channel as its household's
    own item (the member publisher) — no authority copy, nothing
    space-keyed."""
    await _channel_env(env)
    await env["bus"].publish(
        SpacePostCreated(post=_post(env["author_user_id"]), space_id="sp-pub")
    )
    assert env["gfs"].calls == []
    assert env["sub"]._channels.published == []


@pytest.mark.security
async def test_a_member_post_its_author_did_not_put_on_the_channel_goes_there(env):
    skp, channels = await _channel_env(env)
    _author_kp, author_user_id, relay = _remote_relay()
    await env["bus"].publish(
        SpacePostCreated(
            post=_post(author_user_id),
            space_id="sp-pub",
            origin_instance_id="beta.home",
            public_relay=relay,
        )
    )
    # Into the channel only — never ALSO the space-keyed relay.
    assert env["gfs"].calls == []
    assert len(channels.published) == 1
    inner = await _channel_inner(env, skp, channels)
    assert _unpadded(inner) == relay


@pytest.mark.security
async def test_a_member_post_marked_channel_sent_gets_no_host_copy(env):
    _skp, channels = await _channel_env(env)
    _author_kp, author_user_id, relay = _remote_relay()
    await env["bus"].publish(
        SpacePostCreated(
            post=_post(author_user_id),
            space_id="sp-pub",
            origin_instance_id="beta.home",
            public_relay={**relay, CHANNEL_SENT_FIELD: True},
        )
    )
    assert channels.published == []
    assert env["gfs"].calls == []


@pytest.mark.security
@pytest.mark.parametrize("allow_subscribers", [True, False])
async def test_a_removal_goes_into_the_channel_signed_inside(env, allow_subscribers):
    """A removal rides the channel as an authority item — the space
    authority signs the inner inside the ciphertext — whatever
    ``allow_subscribers`` says (the channel carries the members' content)."""
    skp, channels = await _channel_env(env, allow_subscribers=allow_subscribers)
    await env["bus"].publish(
        PostDeleted(
            post_id="post-9",
            space_id="sp-pub",
            actor_user_id="moderator-user",
            author_user_id="author-user",
        )
    )
    assert env["gfs"].calls == []
    inner = await _channel_inner(env, skp, channels)
    assert inner[AUTHORITY_KIND_FIELD] == AUTHORITY_KIND_REMOVAL
    assert inner["item_id"] == "post-9"


@pytest.mark.security
async def test_an_approved_post_goes_into_the_channel_with_its_cert(env):
    skp, author_kp, _iid, post, relay = await _approved_env(env, stype=SpaceType.GLOBAL)
    channels = _Channels({"sp-pub"})
    env["sub"].attach_channels(channels)
    await env["bus"].publish(
        SpacePostCreated(
            post=post, space_id="sp-pub", approved_by="mod", public_relay=relay
        )
    )
    assert env["gfs"].calls == []
    inner = await _channel_inner(env, skp, channels)
    assert is_approved(inner) and verify_signed_author_inner(inner)
    _sid, epoch, _ct = channels.published[-1]
    verify_writer_cert(
        WriterCert.from_wire(inner["writer_cert"]),
        space_pubkey=skp.public_key,
        space_id="sp-pub",
        epoch=epoch,
        author_pk=author_kp.public_key,
        required_scope="comment",
    )
