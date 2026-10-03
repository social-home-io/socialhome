"""Release-blocker protocol tests: space writer certificates (v_49).

Marked ``@pytest.mark.security``.

A writer cert is the space AUTHORITY key's per-epoch statement that one
household may write. It must be impossible to:

* forge one (any key but the pinned space key → refused);
* mint one without the space seed (a member, a follower, a demoted admin
  whose seed no longer matches the pin);
* replay one into another space, or past the epoch it was issued for;
* stretch a comment-scope cert into a post;
* hand one household's cert to another (``instance_pk`` ≠ ``author_pk``);
* store one addressed to a different household.

The receiver tests run the REAL :class:`SpacePublicInbound` over real SQLite
repos: a relayed public post carrying a bad cert never lands, while the same
post with a good cert — or with no cert at all (a pre-v_49 author) — does.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_POST_PUBLIC,
    sign_authority_event,
    strip_authority_sig_fields,
)
from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
    generate_space_keypair,
    sign_ed25519,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import SpacePostCreated
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.post import Post, PostType
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_key_repo import SqliteSpaceKeyRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.space_crypto_service import SpaceContentEncryption
from socialhome.services.space_public_author import (
    author_signing_bytes,
    build_signed_author_inner,
)
from socialhome.services.space_public_inbound import SpacePublicInbound
from socialhome.services.space_public_outbound import SpacePublicOutbound
from socialhome.services.space_writer_cert_service import (
    WRITER_CERT_EPOCH_GRACE_S,
    SpaceWriterCertService,
)
from socialhome.writer_cert import sign_writer_cert

pytestmark = pytest.mark.security

SPACE = "sp-w"
OTHER = "sp-x"


class _Seats:
    def __init__(self, seats: dict[str, list[str]]):
        self.seats = seats

    async def list_for_instance(
        self, space_id, instance_id, *, include_tombstoned=True
    ):
        return [
            SimpleNamespace(role=r, user_id=f"{instance_id}-u{i}")
            for i, r in enumerate(self.seats.get(instance_id, []))
        ]


class _Fed:
    def __init__(self, pks: dict[str, bytes], versions: dict[str, int] | None = None):
        self.pks = pks
        self.versions = versions

    async def peer_supports(self, iid, *, min_version):
        if self.versions is None:
            return True
        return self.versions.get(iid, 0) >= min_version

    async def peer_identity_public_key(self, iid):
        return self.pks.get(iid)


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "w.db", batch_timeout_ms=10)
    await db.startup()
    kek = KeyManager.from_data_dir(tmp_dir)
    spaces = SqliteSpaceRepo(db, key_manager=kek)
    keys = SqliteSpaceKeyRepo(db)
    crypto = SpaceContentEncryption(keys, kek)
    posts = SqliteSpacePostRepo(db)
    space_kp = generate_space_keypair()
    other_kp = generate_space_keypair()
    for sid, kp in ((SPACE, space_kp), (OTHER, other_kp)):
        await spaces.save(
            Space(
                id=sid,
                name="S",
                owner_instance_id="host.home",
                owner_username="h",
                identity_public_key=kp.public_key.hex(),
                config_sequence=0,
                features=SpaceFeatures(),
                space_type=SpaceType.PUBLIC,
                join_mode=JoinMode.OPEN,
            )
        )
        await crypto.initialise_for_space(sid)
    author_kp = generate_identity_keypair()
    inbound = SpacePublicInbound(
        bus=EventBus(), space_repo=spaces, space_crypto=crypto, space_post_repo=posts
    )
    inbound.attach_identity(own_instance_id="us.home")
    # The receiver's writer-cert service: epoch freshness reads its keys.
    holder = SpaceWriterCertService(
        space_repo=spaces,
        remote_member_repo=_Seats({}),
        space_key_repo=keys,
        own_instance_id="us.home",
        own_identity_pk=os.urandom(32),
    )
    inbound.attach_writer_certs(holder)
    yield SimpleNamespace(
        db=db,
        spaces=spaces,
        keys=keys,
        crypto=crypto,
        posts=posts,
        space_kp=space_kp,
        other_kp=other_kp,
        author_kp=author_kp,
        inbound=inbound,
    )
    await db.shutdown()


async def _cert(env, **over) -> dict:
    kw = dict(
        space_seed=env.space_kp.private_key,
        space_id=SPACE,
        epoch=await env.crypto.get_current_epoch(SPACE),
        instance_pk=env.author_kp.public_key,
        scope="write",
    )
    kw.update(over)
    return sign_writer_cert(**kw).to_wire()


async def _relay(env, cert: dict | None, *, post_id: str = "p-1") -> None:
    """Deliver one relayed public post (authority-signed by the host)."""
    await _deliver(env, await _envelope(env, cert, post_id=post_id))


async def _deliver(env, envelope: dict) -> None:
    await env.inbound.handle(
        {"event_type": AUTHORITY_EVENT_SPACE_POST_PUBLIC, "payload": envelope}
    )


async def _envelope(
    env, cert: dict | None, *, post_id: str = "p-1", sign: bool = True
) -> dict:
    """One relayed public post envelope at the CURRENT epoch."""
    uid = derive_user_id(env.author_kp.public_key, "bob")
    inner = {
        "post_id": post_id,
        "space_id": SPACE,
        "author_user_id": uid,
        "author_pk": env.author_kp.public_key.hex(),
        "author_username": "bob",
        "type": "text",
        "content": "hello",
        "created_at": datetime(2026, 10, 3, tzinfo=timezone.utc).isoformat(),
        # The author's household: its id is the fingerprint of the key that
        # signs the inner (the relay path requires exactly that).
        "origin_instance_id": derive_instance_id(env.author_kp.public_key),
    }
    inner["author_sig"] = b64url_encode(
        sign_ed25519(env.author_kp.private_key, author_signing_bytes(inner))
    )
    if cert is not None:
        inner["writer_cert"] = cert
    epoch, ct = await env.crypto.encrypt(SPACE, json.dumps(inner).encode())
    envelope = {"space_id": SPACE, "epoch": epoch, "encrypted_payload": ct}
    if sign:
        envelope.update(
            sign_authority_event(
                event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
                space_id=SPACE,
                payload=strip_authority_sig_fields(envelope),
                space_seed=env.space_kp.private_key,
            )
        )
    return envelope


async def _landed(env, post_id: str = "p-1") -> bool:
    return await env.posts.get(post_id) is not None


# ── Receiver: a relayed item's cert ──────────────────────────────────────


async def test_valid_cert_lands(env):
    await _relay(env, await _cert(env))
    assert await _landed(env)


async def test_pre_v49_author_without_cert_lands(env):
    await _relay(env, None)
    assert await _landed(env)


async def test_forged_cert_is_dropped(env):
    await _relay(env, await _cert(env, space_seed=os.urandom(32)))
    assert not await _landed(env)


async def test_cert_tampered_after_signing_is_dropped(env):
    cert = await _cert(env, scope="comment")
    cert["scope"] = "write"
    await _relay(env, cert)
    assert not await _landed(env)


async def test_cert_from_a_non_seed_holder_is_dropped(env):
    """A member signing with its own household key is no authority."""
    await _relay(env, await _cert(env, space_seed=env.author_kp.private_key))
    assert not await _landed(env)


async def test_cross_space_replay_is_dropped(env):
    """A genuine cert for space X, signed by X's authority, is useless in Y —
    both because it names X and because X's key is not Y's pin."""
    await _relay(
        env, await _cert(env, space_id=OTHER, space_seed=env.other_kp.private_key)
    )
    assert not await _landed(env)
    # Even re-labelled for this space, X's signature does not verify here.
    await _relay(
        env,
        await _cert(env, space_seed=env.other_kp.private_key),
        post_id="p-2",
    )
    assert not await _landed(env, "p-2")


async def test_old_epoch_cert_is_dropped(env):
    old = await _cert(env)
    await env.crypto.rotate_epoch(SPACE)
    await _relay(env, old)
    assert not await _landed(env)


async def test_comment_scope_cert_cannot_post(env):
    await _relay(env, await _cert(env, scope="comment"))
    assert not await _landed(env)


async def test_another_households_cert_is_dropped(env):
    await _relay(
        env, await _cert(env, instance_pk=generate_identity_keypair().public_key)
    )
    assert not await _landed(env)


async def test_unknown_suite_is_dropped(env):
    await _relay(env, {**await _cert(env), "cert_suite": "ed25519+mldsa65"})
    assert not await _landed(env)


# ── Issuer: only a seed holder mints ─────────────────────────────────────


def _issuer(env, *, own_pk: bytes, seats=None, pks=None) -> SpaceWriterCertService:
    svc = SpaceWriterCertService(
        space_repo=env.spaces,
        remote_member_repo=_Seats(seats or {}),
        space_key_repo=env.keys,
        own_instance_id="us.home",
        own_identity_pk=own_pk,
    )
    svc.attach_federation(_Fed(pks or {}))
    return svc


async def test_a_household_without_the_seed_mints_nothing(env):
    svc = _issuer(
        env,
        own_pk=os.urandom(32),
        seats={"author.home": ["member"]},
        pks={"author.home": env.author_kp.public_key},
    )
    assert await svc.issue_for_instance(SPACE, "author.home") is None
    assert await svc.cert_for_peer(SPACE, "author.home") is None


async def test_a_stale_seed_mints_nothing(env):
    """A demoted admin keeps the seed it was given, but after the v_44
    rotation it no longer matches the pin — it must not mint."""
    await env.spaces.set_space_seed(SPACE, generate_space_keypair().private_key)
    svc = _issuer(
        env,
        own_pk=os.urandom(32),
        seats={"author.home": ["member"]},
        pks={"author.home": env.author_kp.public_key},
    )
    assert await svc.issue_for_instance(SPACE, "author.home") is None


async def test_the_seed_holder_mints_a_verifying_cert(env):
    await env.spaces.set_space_seed(SPACE, env.space_kp.private_key)
    svc = _issuer(
        env,
        own_pk=os.urandom(32),
        seats={"author.home": ["member"]},
        pks={"author.home": env.author_kp.public_key},
    )
    wire = await svc.cert_for_peer(SPACE, "author.home")
    assert wire is not None
    await _relay(env, wire)
    assert await _landed(env)


async def test_a_follower_cannot_get_a_write_cert(env):
    await env.spaces.set_space_seed(SPACE, env.space_kp.private_key)
    svc = _issuer(
        env,
        own_pk=os.urandom(32),
        seats={"author.home": ["subscriber"]},
        pks={"author.home": env.author_kp.public_key},
    )
    assert await svc.issue_for_instance(SPACE, "author.home") is None


# ── Holder: only our own cert is stored ──────────────────────────────────


async def test_a_holder_stores_only_a_cert_naming_it(env):
    me = generate_identity_keypair()
    holder = _issuer(env, own_pk=me.public_key)
    epoch = await env.crypto.get_current_epoch(SPACE)
    # Another household's genuine cert.
    assert not await holder.accept(SPACE, await _cert(env))
    # Ours, but forged / for another space / for an epoch we hold no key for.
    assert not await holder.accept(
        SPACE, await _cert(env, instance_pk=me.public_key, space_seed=os.urandom(32))
    )
    assert not await holder.accept(
        SPACE,
        await _cert(
            env,
            instance_pk=me.public_key,
            space_id=OTHER,
            space_seed=env.other_kp.private_key,
        ),
    )
    assert not await holder.accept(
        SPACE, await _cert(env, instance_pk=me.public_key, epoch=epoch + 50)
    )
    assert await env.keys.get_writer_cert(SPACE, epoch) is None
    # The genuine one is kept.
    assert await holder.accept(SPACE, await _cert(env, instance_pk=me.public_key))
    assert await env.keys.get_writer_cert(SPACE, epoch) is not None


# ── Adversarial review (second pass) ─────────────────────────────────────


async def test_no_cert_and_no_authority_signature_never_lands(env):
    """Neither a cert nor an authority signature → dropped."""
    await _deliver(env, await _envelope(env, None, sign=False))
    assert not await _landed(env)
    await _deliver(env, await _envelope(env, await _cert(env), sign=False))
    assert not await _landed(env)


async def test_host_signed_item_at_an_old_epoch_still_lands(env):
    """A host-signed item (the host re-stamped it at ITS current epoch) that
    arrives after later rotations is late delivery, not revocation: the
    authority signature is the authorizer, so no freshness gate."""
    envelope = await _envelope(env, await _cert(env))
    await env.crypto.rotate_epoch(SPACE)
    await env.crypto.rotate_epoch(SPACE)
    await _deliver(env, envelope)
    assert await _landed(env)


async def _cert_item_ok(env, cert: dict, epoch: int) -> bool:
    """The member-authorized (cert-only) check PR 2 relies on."""
    return await env.inbound._writer_certs.check_item(
        await env.spaces.get(SPACE),
        cert,
        epoch=epoch,
        author_pk=env.author_kp.public_key,
        required_scope="write",
    )


async def test_cert_only_item_previous_epoch_open_within_the_grace(env):
    e0 = await env.crypto.get_current_epoch(SPACE)
    cert = await _cert(env)
    await env.crypto.rotate_epoch(SPACE)
    assert await _cert_item_ok(env, cert, e0)


async def test_cert_only_item_previous_epoch_closed_after_the_grace(env):
    """R5: an old-epoch cert does not authorize a member item forever."""
    e0 = await env.crypto.get_current_epoch(SPACE)
    cert = await _cert(env)
    new_epoch = await env.crypto.rotate_epoch(SPACE)
    old = datetime.now(timezone.utc) - timedelta(seconds=WRITER_CERT_EPOCH_GRACE_S + 60)
    await env.db.enqueue(
        "UPDATE space_keys SET created_at=? WHERE space_id=? AND epoch=?",
        (old.isoformat(), SPACE, new_epoch),
    )
    assert not await _cert_item_ok(env, cert, e0)


async def test_cert_only_item_two_epochs_back_is_closed(env):
    e0 = await env.crypto.get_current_epoch(SPACE)
    cert = await _cert(env)
    await env.crypto.rotate_epoch(SPACE)
    await env.crypto.rotate_epoch(SPACE)
    assert not await _cert_item_ok(env, cert, e0)


async def test_a_weaker_older_cert_never_replaces_ours(env):
    """R2: a replayed older comment cert does not overwrite our write cert."""
    me = generate_identity_keypair()
    holder = _issuer(env, own_pk=me.public_key)
    epoch = await env.crypto.get_current_epoch(SPACE)
    write = await _cert(env, instance_pk=me.public_key, issued_at=200)
    comment = await _cert(
        env, instance_pk=me.public_key, scope="comment", issued_at=100
    )
    assert await holder.accept(SPACE, write)
    await holder.accept(SPACE, comment)
    assert (await holder.own_cert(SPACE, epoch)).scope == "write"  # type: ignore[union-attr]
    # Same issued_at: write is preferred.
    tie = await _cert(env, instance_pk=me.public_key, scope="comment", issued_at=200)
    await holder.accept(SPACE, tie)
    assert (await holder.own_cert(SPACE, epoch)).scope == "write"  # type: ignore[union-attr]
    # A genuinely newer cert does replace it.
    newer = await _cert(env, instance_pk=me.public_key, scope="comment", issued_at=300)
    assert await holder.accept(SPACE, newer)
    assert (await holder.own_cert(SPACE, epoch)).scope == "comment"  # type: ignore[union-attr]


async def test_own_cert_signed_by_a_retired_key_is_not_handed_out(env):
    """R4: after a re-pin, the stored cert (old key) is not ours any more."""
    me = generate_identity_keypair()
    holder = _issuer(env, own_pk=me.public_key)
    epoch = await env.crypto.get_current_epoch(SPACE)
    assert await holder.accept(SPACE, await _cert(env, instance_pk=me.public_key))
    k2 = generate_space_keypair()
    await env.db.enqueue(
        "UPDATE spaces SET identity_public_key=? WHERE id=?",
        (k2.public_key.hex(), SPACE),
    )
    assert await holder.own_cert(SPACE, epoch) is None
    # A seed holder for the NEW key self-issues instead.
    await env.spaces.set_space_seed(SPACE, k2.private_key)
    await env.db.enqueue(
        "INSERT OR IGNORE INTO space_members(space_id, user_id, role, joined_at)"
        " VALUES(?, 'me', 'owner', '2026-01-01')",
        (SPACE,),
    )
    fresh = await holder.own_cert(SPACE, epoch)
    assert fresh is not None and fresh.instance_pk == b64url_encode(me.public_key)


# ── Host relay (SpacePublicOutbound) ─────────────────────────────────────


class _CaptureGfs:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def publish_space_event(self, *, space_id, event_type, payload) -> int:
        self.calls.append(payload)
        return 1


@pytest.fixture
async def host(env):
    """The space's host: holds the seed and relays remote-authored posts."""
    await env.spaces.set_space_seed(SPACE, env.space_kp.private_key)
    space = await env.spaces.get(SPACE)
    await env.spaces.save(
        replace(space, features=replace(space.features, allow_subscribers=True))
    )
    gfs = _CaptureGfs()
    out = SpacePublicOutbound(
        bus=EventBus(),
        space_repo=env.spaces,
        space_crypto=env.crypto,
        user_repo=SimpleNamespace(),
        gfs_service=gfs,
    )
    host_kp = generate_identity_keypair()
    out.attach_identity(
        own_instance_id="host.home",
        own_instance_public_key=host_kp.public_key,
        own_identity_seed=host_kp.private_key,
    )

    def _certs(seats, pks, versions=None):
        svc = SpaceWriterCertService(
            space_repo=env.spaces,
            remote_member_repo=_Seats(seats),
            space_key_repo=env.keys,
            own_instance_id="host.home",
            own_identity_pk=host_kp.public_key,
        )
        svc.attach_federation(_Fed(pks, versions or {}))
        out.attach_writer_certs(svc)

    return SimpleNamespace(out=out, gfs=gfs, certs=_certs)


def _relay_hint(env, *, cert: dict | None) -> dict:
    uid = derive_user_id(env.author_kp.public_key, "bob")
    post = Post(
        id="p-h",
        author=uid,
        type=PostType.TEXT,
        content="hi",
        created_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
    )
    inner = build_signed_author_inner(
        post=post,
        space_id=SPACE,
        author_username="bob",
        author_pk=env.author_kp.public_key,
        author_identity_seed=env.author_kp.private_key,
        origin_instance_id="author.home",
    )
    if cert is not None:
        inner["writer_cert"] = cert
    return inner


async def _host_relays(env, host, inner) -> bool:
    await host.out._on_space_post_created(
        SpacePostCreated(
            post=Post(
                id="p-h",
                author=inner["author_user_id"],
                type=PostType.TEXT,
                content="hi",
                created_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
            ),
            space_id=SPACE,
            origin_instance_id="author.home",
            public_relay=inner,
        )
    )
    return bool(host.gfs.calls)


async def test_host_never_restamps_a_comment_cert_onto_a_post(env, host):
    """R1: the household now holds only a follower seat (comments on) — the
    host must not mint a comment cert onto its post."""
    space = await env.spaces.get(SPACE)
    await env.spaces.save(
        replace(space, features=replace(space.features, allow_subscriber_comment=True))
    )
    host.certs(
        {"author.home": ["subscriber"]}, {"author.home": env.author_kp.public_key}
    )
    assert not await _host_relays(env, host, _relay_hint(env, cert=await _cert(env)))


async def test_host_restamps_a_live_writer(env, host):
    host.certs({"author.home": ["member"]}, {"author.home": env.author_kp.public_key})
    assert await _host_relays(env, host, _relay_hint(env, cert=await _cert(env)))


async def test_stripping_the_cert_does_not_bypass_the_host(env, host):
    """R3: a v_49 origin's hint without a cert is refused; a legacy hint is
    bound to the origin's pinned key."""
    v49 = FederationCapability.MIN_FOR_MEMBER_GFS_PUBLISH
    host.certs({}, {"author.home": env.author_kp.public_key}, {"author.home": v49})
    assert not await _host_relays(env, host, _relay_hint(env, cert=None))


async def test_legacy_hint_must_name_the_origins_key(env, host):
    host.certs({}, {"author.home": generate_space_keypair().public_key})
    assert not await _host_relays(env, host, _relay_hint(env, cert=None))


async def test_legacy_hint_from_a_pre_v49_origin_still_relays(env, host):
    host.certs({}, {"author.home": env.author_kp.public_key}, {"author.home": 48})
    assert await _host_relays(env, host, _relay_hint(env, cert=None))


# ── Round 3: mesh-only origins (R7) ──────────────────────────────────────


async def _mesh_relays(env, host, inner, origin: str) -> bool:
    await host.out._on_space_post_created(
        SpacePostCreated(
            post=Post(
                id="p-h",
                author=inner["author_user_id"],
                type=PostType.TEXT,
                content="hi",
                created_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
            ),
            space_id=SPACE,
            origin_instance_id=origin,
            public_relay=inner,
        )
    )
    return bool(host.gfs.calls)


async def test_mesh_only_origin_legacy_hint_still_relays(env, host):
    """R7: a mesh-only member (no remote_instances row, unknown version, no
    pinned key) relays on the host's authority as on main — its author key
    bound by ``derive_instance_id(author_pk) == origin``, the self-
    authentication the v_31 routed-origin check verified it with."""
    origin = derive_instance_id(env.author_kp.public_key)
    host.certs({origin: ["member"]}, {}, {})
    assert await _mesh_relays(env, host, _relay_hint(env, cert=None), origin)


async def test_mesh_only_origin_with_a_cert_is_restamped(env, host):
    origin = derive_instance_id(env.author_kp.public_key)
    host.certs({origin: ["member"]}, {}, {})
    assert await _mesh_relays(
        env, host, _relay_hint(env, cert=await _cert(env)), origin
    )


async def test_mesh_only_origin_with_someone_elses_key_is_refused(env, host):
    origin = derive_instance_id(generate_identity_keypair().public_key)
    host.certs({origin: ["member"]}, {}, {})
    assert not await _mesh_relays(env, host, _relay_hint(env, cert=None), origin)
