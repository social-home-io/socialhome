"""Tests for :class:`SpacePublicInbound` (Phase 5a2 consumer).

A relayed ``space_post_public`` envelope is verified (authority sig vs the
locally-mirrored space public key), decrypted under the per-space content
key, the author is self-certified, deduped by post id, and persisted.

Security drops (defence-in-depth — the GFS already verified, but the relay
is never trusted): bad/forged authority sig, author self-cert mismatch,
missing content key (epoch not held).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pytest

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_POST_PUBLIC,
    sign_authority_event,
    strip_authority_sig_fields,
)
from socialhome.crypto import (
    b64url_encode,
    derive_user_id,
    generate_identity_keypair,
    generate_space_keypair,
    sign_ed25519,
)
from socialhome.services.space_public_author import (
    author_signing_bytes,
    link_preview_signing_bytes,
)
from socialhome.federation.owner_bound_id import SPACE_POST_KIND, mint_owner_bound_id
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import SpacePostCreated
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceType,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_key_repo import SqliteSpaceKeyRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_mentions import SpaceMentionResolver
from socialhome.services.space_crypto_service import SpaceContentEncryption
from socialhome.services.space_public_inbound import SpacePublicInbound
from socialhome.writer_cert import sign_writer_cert


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    kek = KeyManager.from_data_dir(tmp_dir)
    space_repo = SqliteSpaceRepo(db, key_manager=kek)
    key_repo = SqliteSpaceKeyRepo(db)
    crypto = SpaceContentEncryption(key_repo, kek)
    post_repo = SqliteSpacePostRepo(db)
    bus = EventBus()

    # The remote author household's identity (the one that signed the post).
    author_kp = generate_identity_keypair()
    author_user_id = derive_user_id(author_kp.public_key, "bob")
    # The space identity (seed lives on the relaying household, only the
    # public key is mirrored locally here).
    space_kp = generate_space_keypair()

    await space_repo.save(
        Space(
            id="sp-1",
            name="S",
            owner_instance_id="remote.home",
            owner_username="bob",
            identity_public_key=space_kp.public_key.hex(),
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.PUBLIC,
            join_mode=JoinMode.OPEN,
        )
    )
    await crypto.initialise_for_space("sp-1")

    events: list[SpacePostCreated] = []

    async def _record(e: SpacePostCreated) -> None:
        events.append(e)

    bus.subscribe(SpacePostCreated, _record)

    inbound = SpacePublicInbound(
        bus=bus,
        space_repo=space_repo,
        space_crypto=crypto,
        space_post_repo=post_repo,
    )
    inbound.attach_identity(own_instance_id="us.home")
    return {
        "db": db,
        "space_repo": space_repo,
        "crypto_obj": crypto,
        "own_iid": "us.home",
        "bus": bus,
        "crypto": crypto,
        "post_repo": post_repo,
        "inbound": inbound,
        "author_kp": author_kp,
        "author_user_id": author_user_id,
        "space_kp": space_kp,
        "events": events,
    }


async def _make_envelope(
    env,
    *,
    space_id: str = "sp-1",
    post_id: str = "post-1",
    author_user_id: str | None = None,
    author_pk: bytes | None = None,
    author_sign_seed: bytes | None = None,
    omit_author_sig: bool = False,
    space_seed: bytes | None = None,
    identity_anchor: str | None = None,
    content: str = "secret space content",
    link_preview: dict | None = None,
    tamper_preview: dict | None = None,
    preview_suite: str = "ed25519",
    writer_cert: dict | None = None,
):
    """Build a relayed envelope.

    By default the inner ``author_sig`` is produced by the real author's
    identity seed (``author_kp.private_key``). ``author_sign_seed`` overrides
    the signing key (to forge a sig from a different key); ``omit_author_sig``
    drops it entirely.
    """
    author_user_id = author_user_id or env["author_user_id"]
    author_pk = author_pk if author_pk is not None else env["author_kp"].public_key
    space_seed = space_seed if space_seed is not None else env["space_kp"].private_key
    sign_seed = (
        author_sign_seed
        if author_sign_seed is not None
        else env["author_kp"].private_key
    )
    inner = {
        "post_id": post_id,
        "space_id": space_id,
        "author_user_id": author_user_id,
        "author_pk": author_pk.hex(),
        "author_username": "bob",
        "type": "text",
        "content": content,
        "created_at": datetime(2026, 6, 10, tzinfo=timezone.utc).isoformat(),
        "origin_instance_id": "remote.home",
    }
    if identity_anchor is not None:
        inner["identity_anchor"] = identity_anchor
    if not omit_author_sig:
        inner["author_sig"] = b64url_encode(
            sign_ed25519(sign_seed, author_signing_bytes(inner))
        )
    if link_preview is not None:
        inner["link_preview"] = link_preview
        inner["link_preview_sig_suite"] = preview_suite
        inner["link_preview_sig"] = b64url_encode(
            sign_ed25519(sign_seed, link_preview_signing_bytes(inner))
        )
        if tamper_preview is not None:
            inner["link_preview"] = tamper_preview
    if writer_cert is not None:
        inner["writer_cert"] = writer_cert
    epoch, ct = await env["crypto"].encrypt(space_id, json.dumps(inner).encode())
    envelope = {"space_id": space_id, "epoch": epoch, "encrypted_payload": ct}
    envelope.update(
        sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            space_id=space_id,
            payload=strip_authority_sig_fields(envelope),
            space_seed=space_seed,
        )
    )
    return envelope


def _frame(envelope: dict) -> dict:
    return {
        "type": "relay",
        "space_id": envelope["space_id"],
        "event_type": AUTHORITY_EVENT_SPACE_POST_PUBLIC,
        "payload": envelope,
        "from_instance": "remote.home",
    }


async def test_valid_relay_decrypts_persists_and_publishes(env):
    envelope = await _make_envelope(env)
    await env["inbound"].handle(_frame(envelope), gfs_id="g1")
    got = await env["post_repo"].get("post-1")
    assert got is not None
    space_id, post = got
    assert space_id == "sp-1"
    assert post.content == "secret space content"
    assert post.author == env["author_user_id"]
    assert len(env["events"]) == 1
    assert env["events"][0].origin_instance_id == "remote.home"


async def test_relayed_post_resolves_mentions_against_local_member_view(env):
    """The subscriber household parses the decrypted relayed post against
    its own member view — a member resolves, a non-member never does."""
    for uname in ("anna", "dave"):
        await env["db"].enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (uname, f"u-{uname}", uname.title()),
        )
    await env["space_repo"].save_member(
        SpaceMember(space_id="sp-1", user_id="u-anna", role="member", joined_at="")
    )
    inbound = SpacePublicInbound(
        bus=env["bus"],
        space_repo=env["space_repo"],
        space_crypto=env["crypto_obj"],
        space_post_repo=env["post_repo"],
        mention_resolver=SpaceMentionResolver(
            env["space_repo"], SqliteUserRepo(env["db"])
        ),
    )
    inbound.attach_identity(own_instance_id="us.home")
    envelope = await _make_envelope(env, content="hi @anna and @dave")
    await inbound.handle(_frame(envelope), gfs_id="g1")
    assert [m.user_id for m in env["events"][0].mentions] == ["u-anna"]


async def test_relay_without_resolver_carries_no_mentions(env):
    envelope = await _make_envelope(env, content="hi @anna")
    await env["inbound"].handle(_frame(envelope), gfs_id="g1")
    assert env["events"][0].mentions == ()


async def test_dedupe_same_envelope_twice_persists_once(env):
    envelope = await _make_envelope(env)
    await env["inbound"].handle(_frame(envelope), gfs_id="g1")
    await env["inbound"].handle(_frame(envelope), gfs_id="g1")
    got = await env["post_repo"].get("post-1")
    assert got is not None
    # The second delivery is dropped — only one bus event published.
    assert len(env["events"]) == 1


async def test_forged_authority_sig_dropped(env):
    envelope = await _make_envelope(env)
    # Tamper the signature.
    envelope["authority_sig"] = "AAAA" + envelope["authority_sig"][4:]
    await env["inbound"].handle(_frame(envelope), gfs_id="g1")
    assert await env["post_repo"].get("post-1") is None
    assert env["events"] == []


async def test_wrong_space_seed_sig_dropped(env):
    """An envelope signed by a DIFFERENT seed (not the space's) → drop."""
    other = generate_space_keypair()
    envelope = await _make_envelope(env, space_seed=other.private_key)
    await env["inbound"].handle(_frame(envelope), gfs_id="g1")
    assert await env["post_repo"].get("post-1") is None
    assert env["events"] == []


async def test_author_self_cert_mismatch_dropped(env):
    """author_pk that doesn't derive to author_user_id → drop."""
    impostor = generate_identity_keypair()
    # Sign correctly with the space seed, but the inner author_pk is the
    # impostor's key while author_user_id stays the real bob.
    envelope = await _make_envelope(env, author_pk=impostor.public_key)
    await env["inbound"].handle(_frame(envelope), gfs_id="g1")
    assert await env["post_repo"].get("post-1") is None
    assert env["events"] == []


async def test_a_bound_post_id_claimed_by_another_author_is_dropped(env, caplog):
    """v_36: a post id commits to its author. Another household's user can
    sign a valid inner for an id it has seen, but not one bound to itself —
    the claim is dropped, and the real author's post still lands."""
    post_id = mint_owner_bound_id(
        SPACE_POST_KIND, space_id="sp-1", owner_user_id=env["author_user_id"]
    )
    eve = generate_identity_keypair()
    eve_user_id = derive_user_id(eve.public_key, "bob")
    squat = await _make_envelope(
        env,
        post_id=post_id,
        author_user_id=eve_user_id,
        author_pk=eve.public_key,
        author_sign_seed=eve.private_key,
    )
    with caplog.at_level(logging.WARNING):
        await env["inbound"].handle(_frame(squat), gfs_id="g1")
    assert await env["post_repo"].get(post_id) is None
    assert "not bound to" in caplog.text
    genuine = await _make_envelope(env, post_id=post_id)
    await env["inbound"].handle(_frame(genuine), gfs_id="g1")
    got = await env["post_repo"].get(post_id)
    assert got is not None and got[1].author == env["author_user_id"]


async def test_anchor_derived_author_accepted(env):
    """An author whose ``author_user_id`` derives from a uuid ``identity_anchor``
    (NOT the username) is accepted when the anchor is carried + signed."""
    anchor = "2f3c9d1e4b5a6789abcdef0123456789"
    author_kp = env["author_kp"]
    anchored_user_id = derive_user_id(author_kp.public_key, anchor)
    # Sanity: username derivation would NOT yield this id.
    assert derive_user_id(author_kp.public_key, "bob") != anchored_user_id
    envelope = await _make_envelope(
        env, author_user_id=anchored_user_id, identity_anchor=anchor
    )
    await env["inbound"].handle(_frame(envelope), gfs_id="g1")
    got = await env["post_repo"].get("post-1")
    assert got is not None
    _space_id, post = got
    assert post.author == anchored_user_id
    assert len(env["events"]) == 1


async def test_forged_anchor_dropped(env):
    """A wrong ``identity_anchor`` (user_id no longer derives from it) → drop.
    The anchor is part of the signed bytes, so author_sig also breaks."""
    anchor = "2f3c9d1e4b5a6789abcdef0123456789"
    author_kp = env["author_kp"]
    anchored_user_id = derive_user_id(author_kp.public_key, anchor)
    envelope = await _make_envelope(
        env,
        author_user_id=anchored_user_id,
        identity_anchor="ffffffffffffffffffffffffffffffff",
    )
    await env["inbound"].handle(_frame(envelope), gfs_id="g1")
    assert await env["post_repo"].get("post-1") is None
    assert env["events"] == []


async def test_impersonation_no_author_sig_dropped(env):
    """A seed-holder relays an envelope claiming the victim's
    author_user_id/author_pk but carries NO author_sig → DROPPED.

    Self-cert (pk↔user_id) passes — both are public — so without a per-author
    signature this is an impersonation. Must not persist.
    """
    envelope = await _make_envelope(env, omit_author_sig=True)
    await env["inbound"].handle(_frame(envelope), gfs_id="g1")
    assert await env["post_repo"].get("post-1") is None
    assert env["events"] == []


async def test_impersonation_author_sig_wrong_key_dropped(env):
    """An author_sig signed by a DIFFERENT key than author_pk → DROPPED.

    A malicious seed-holder can't mint a valid author_sig for a victim's pk
    (it lacks the victim's identity seed), so any sig it can produce is over
    its own key and fails verification against the claimed author_pk.
    """
    impostor = generate_identity_keypair()
    # author_pk stays the real bob (self-cert passes), but the sig is made
    # with the impostor's seed — verification against author_pk fails.
    envelope = await _make_envelope(env, author_sign_seed=impostor.private_key)
    await env["inbound"].handle(_frame(envelope), gfs_id="g1")
    assert await env["post_repo"].get("post-1") is None
    assert env["events"] == []


async def test_malformed_author_sig_dropped(env):
    """A non-base64 / garbage author_sig → DROPPED, no crash."""
    envelope = await _make_envelope(env, omit_author_sig=True)
    # Inject a malformed author_sig into the (already-encrypted) inner: rebuild.
    inner = {
        "post_id": "post-1",
        "author_user_id": env["author_user_id"],
        "author_pk": env["author_kp"].public_key.hex(),
        "author_username": "bob",
        "type": "text",
        "content": "secret space content",
        "created_at": datetime(2026, 6, 10, tzinfo=timezone.utc).isoformat(),
        "origin_instance_id": "remote.home",
        "author_sig": "!!!not base64!!!",
    }
    epoch, ct = await env["crypto"].encrypt("sp-1", json.dumps(inner).encode())
    envelope = {"space_id": "sp-1", "epoch": epoch, "encrypted_payload": ct}
    envelope.update(
        sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            space_id="sp-1",
            payload=strip_authority_sig_fields(envelope),
            space_seed=env["space_kp"].private_key,
        )
    )
    await env["inbound"].handle(_frame(envelope), gfs_id="g1")
    assert await env["post_repo"].get("post-1") is None
    assert env["events"] == []


async def test_valid_author_sig_round_trip_persists(env):
    """An envelope with a valid per-author signature → verified + persisted."""
    envelope = await _make_envelope(env)
    await env["inbound"].handle(_frame(envelope), gfs_id="g1")
    got = await env["post_repo"].get("post-1")
    assert got is not None
    assert len(env["events"]) == 1


async def test_tampered_ciphertext_invalid_tag_dropped(env):
    """A base64-valid but TAMPERED encrypted_payload raises InvalidTag inside
    aead.decrypt — must be caught and dropped gracefully, no exception
    escapes, nothing persisted."""
    envelope = await _make_envelope(env)
    ct = envelope["encrypted_payload"]
    # Flip a byte in the base64 ciphertext while keeping it base64-decodable.
    # The format is "<nonce_b64>:<ct_b64>"; mutate the ct portion.
    head, _, tail = ct.partition(":")
    assert tail, "expected nonce:ct ciphertext shape"
    flipped = ("B" if tail[0] != "B" else "C") + tail[1:]
    envelope["encrypted_payload"] = f"{head}:{flipped}"
    # Re-sign the envelope so the authority check passes and we reach decrypt.
    envelope = {
        k: v
        for k, v in envelope.items()
        if k not in ("authority_sig", "authority_sig_suite")
    }
    envelope.update(
        sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            space_id="sp-1",
            payload=strip_authority_sig_fields(envelope),
            space_seed=env["space_kp"].private_key,
        )
    )
    # Must not raise.
    await env["inbound"].handle(_frame(envelope), gfs_id="g1")
    assert await env["post_repo"].get("post-1") is None
    assert env["events"] == []


async def test_missing_content_key_dropped_gracefully(env):
    """Envelope references an epoch this household doesn't hold → drop,
    no crash, no persist (subscribers get the key in Phase 5b)."""
    envelope = await _make_envelope(env)
    # Bump the epoch to one with no key.
    envelope["epoch"] = 99
    # Re-sign so the authority check passes and we exercise the decrypt drop.
    envelope = {
        k: v
        for k, v in envelope.items()
        if k not in ("authority_sig", "authority_sig_suite")
    }
    envelope.update(
        sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            space_id="sp-1",
            payload=strip_authority_sig_fields(envelope),
            space_seed=env["space_kp"].private_key,
        )
    )
    await env["inbound"].handle(_frame(envelope), gfs_id="g1")
    assert await env["post_repo"].get("post-1") is None
    assert env["events"] == []


async def test_unknown_local_space_dropped(env):
    """An envelope for a space we don't mirror locally → drop (no pubkey
    to verify against, no key to decrypt)."""
    envelope = {
        "space_id": "sp-unknown",
        "epoch": 0,
        "encrypted_payload": "x:y",
        "authority_sig": "AAAA",
        "authority_sig_suite": "ed25519",
    }
    await env["inbound"].handle(_frame(envelope), gfs_id="g1")
    assert env["events"] == []


async def test_cross_space_inner_dropped(env):
    """A relayed envelope for space "sp-1" whose decrypted inner is validly
    signed for a DIFFERENT space ("sp-other") → cross-space injection. The
    subscriber drops it (post not persisted, no event published) — the inner's
    signed ``space_id`` must equal the outer envelope ``space_id``."""
    # Build an inner author-signed for "sp-other" (a valid, self-certifying
    # author sig — only the space_id differs from the envelope below).
    inner = {
        "post_id": "post-1",
        "space_id": "sp-other",
        "author_user_id": env["author_user_id"],
        "author_pk": env["author_kp"].public_key.hex(),
        "author_username": "bob",
        "type": "text",
        "content": "secret space content",
        "created_at": datetime(2026, 6, 10, tzinfo=timezone.utc).isoformat(),
        "origin_instance_id": "remote.home",
    }
    inner["author_sig"] = b64url_encode(
        sign_ed25519(env["author_kp"].private_key, author_signing_bytes(inner))
    )
    # Encrypt + authority-sign under the OUTER space ("sp-1").
    epoch, ct = await env["crypto"].encrypt("sp-1", json.dumps(inner).encode())
    envelope = {"space_id": "sp-1", "epoch": epoch, "encrypted_payload": ct}
    envelope.update(
        sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            space_id="sp-1",
            payload=strip_authority_sig_fields(envelope),
            space_seed=env["space_kp"].private_key,
        )
    )
    await env["inbound"].handle(_frame(envelope), gfs_id="g1")
    assert await env["post_repo"].get("post-1") is None
    assert env["events"] == []


async def test_wrong_event_type_ignored(env):
    envelope = await _make_envelope(env)
    frame = _frame(envelope)
    frame["event_type"] = "some_other_event"
    await env["inbound"].handle(frame, gfs_id="g1")
    assert await env["post_repo"].get("post-1") is None
    assert env["events"] == []


# ── Identity-free GFS fan-out (the GFS never sees which household relayed) ──


def _anonymous_frame(envelope: dict) -> dict:
    """The identity-free fan-out frame the GFS now pushes — exactly four keys,
    no ``from_instance``."""
    return {
        "type": "relay",
        "space_id": envelope["space_id"],
        "event_type": AUTHORITY_EVENT_SPACE_POST_PUBLIC,
        "payload": envelope,
    }


async def test_frame_without_from_instance_uses_inner_origin(env):
    """A four-key frame (no ``from_instance``) is processed normally and the
    published origin comes from the encrypted, authority-signed inner."""
    envelope = await _make_envelope(env)
    await env["inbound"].handle(_anonymous_frame(envelope), gfs_id="g1")
    got = await env["post_repo"].get("post-1")
    assert got is not None
    assert got[1].content == "secret space content"
    assert len(env["events"]) == 1
    assert env["events"][0].origin_instance_id == "remote.home"


async def test_spoofed_outer_from_instance_is_ignored(env, caplog):
    """A legacy/hostile frame whose outer ``from_instance`` names a victim must
    never become the attribution — the inner wins and the spoofed value lands
    in no stored field and no log record."""
    caplog.set_level(logging.DEBUG, logger="socialhome")
    envelope = await _make_envelope(env)
    frame = _anonymous_frame(envelope)
    frame["from_instance"] = "victim.home"
    await env["inbound"].handle(frame, gfs_id="g1")

    got = await env["post_repo"].get("post-1")
    assert got is not None
    assert len(env["events"]) == 1
    assert env["events"][0].origin_instance_id == "remote.home"
    assert "victim.home" not in repr(got)
    leaked = [
        r.getMessage()
        for r in caplog.records
        if r.name.startswith("socialhome") and "victim.home" in r.getMessage()
    ]
    assert leaked == []


async def test_self_echo_dropped_before_any_write(env, caplog):
    """The GFS no longer excludes the publisher from its own fan-out, so our
    own post comes back to us. An inner origin equal to our own instance id is
    dropped at DEBUG before any persist."""
    caplog.set_level(logging.DEBUG, logger="socialhome")
    inner = {
        "post_id": "post-echo",
        "space_id": "sp-1",
        "author_user_id": env["author_user_id"],
        "author_pk": env["author_kp"].public_key.hex(),
        "author_username": "bob",
        "type": "text",
        "content": "our own post, echoed back",
        "created_at": datetime(2026, 6, 10, tzinfo=timezone.utc).isoformat(),
        "origin_instance_id": env["own_iid"],
    }
    inner["author_sig"] = b64url_encode(
        sign_ed25519(env["author_kp"].private_key, author_signing_bytes(inner))
    )
    epoch, ct = await env["crypto"].encrypt("sp-1", json.dumps(inner).encode())
    envelope = {"space_id": "sp-1", "epoch": epoch, "encrypted_payload": ct}
    envelope.update(
        sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            space_id="sp-1",
            payload=strip_authority_sig_fields(envelope),
            space_seed=env["space_kp"].private_key,
        )
    )

    await env["inbound"].handle(_anonymous_frame(envelope), gfs_id="g1")

    assert await env["post_repo"].get("post-echo") is None
    assert env["events"] == []
    noisy = [
        r
        for r in caplog.records
        if r.name.startswith("socialhome") and r.levelno > logging.DEBUG
    ]
    assert noisy == []


_CARD = {
    "url": "https://example.com/a",
    "title": "Card",
    "description": "Desc",
    "site_name": None,
    "thumbnail_url": "api/media/lp.webp",
}


async def test_relayed_link_preview_kept_when_author_signed(env):
    envelope = await _make_envelope(env, link_preview=_CARD)
    await env["inbound"].handle(_frame(envelope), gfs_id="g1")
    _, post = await env["post_repo"].get("post-1")
    assert post.link_preview is not None
    assert post.link_preview.title == "Card"
    assert post.link_preview.thumbnail_url == "api/media/lp.webp"


async def test_relayed_link_preview_altered_by_relayer_dropped(env):
    envelope = await _make_envelope(
        env,
        link_preview=_CARD,
        tamper_preview={**_CARD, "url": "https://evil.example/"},
    )
    await env["inbound"].handle(_frame(envelope), gfs_id="g1")
    got = await env["post_repo"].get("post-1")
    assert got is not None  # the post still lands
    assert got[1].link_preview is None


async def test_relayed_link_preview_unknown_suite_dropped(env, caplog):
    envelope = await _make_envelope(env, link_preview=_CARD, preview_suite="future")
    with caplog.at_level("WARNING"):
        await env["inbound"].handle(_frame(envelope), gfs_id="g1")
    got = await env["post_repo"].get("post-1")
    assert got is not None and got[1].link_preview is None
    assert "link preview dropped" in caplog.text


@pytest.mark.parametrize("reason", [None, "dissolved"])
async def test_a_relayed_post_into_an_archived_space_is_dropped(env, reason):
    """An archived space is a read-only snapshot on every inbound door; the
    relay frame names no household, so not even the host is exempt."""
    await env["space_repo"].set_archived("sp-1", True, reason=reason)
    envelope = await _make_envelope(env)
    await env["inbound"].handle(_frame(envelope), gfs_id="g1")
    assert await env["post_repo"].get("post-1") is None
    assert env["events"] == []


# ── v_44: lazy pin heal on an authority failure ──────────────────────────


class _Refresher:
    """Stands in for the GFS mirror's pin refresh: re-pins to ``new_pk``."""

    def __init__(self, space_repo, new_pk_hex: str | None):
        self._spaces = space_repo
        self._new = new_pk_hex
        self.calls: list[str] = []

    async def refresh_authority_pin(self, space_id: str) -> bool:
        self.calls.append(space_id)
        if self._new is None:
            return False
        await self._spaces.adopt_authority_key(space_id, self._new, 1)
        return True


async def test_new_key_post_heals_the_pin_and_is_accepted(env):
    """The owner rotated; this subscriber still pins K1. A K2-signed post
    fails, the refresh re-pins from the GFS cert, the retry accepts it."""
    k2 = generate_space_keypair()
    refresher = _Refresher(env["space_repo"], k2.public_key.hex())
    env["inbound"].attach_pin_refresher(refresher)
    env_frame = _frame(await _make_envelope(env, space_seed=k2.private_key))
    await env["inbound"].handle(env_frame)
    assert refresher.calls == ["sp-1"]
    assert await env["post_repo"].get("post-1") is not None


async def test_old_key_post_is_refused_after_the_heal(env):
    """Spec §8.5: once healed to K2, a K1-signed relayed post is refused."""
    k1_seed = env["space_kp"].private_key
    k2 = generate_space_keypair()
    await env["space_repo"].adopt_authority_key("sp-1", k2.public_key.hex(), 1)
    refresher = _Refresher(env["space_repo"], None)
    env["inbound"].attach_pin_refresher(refresher)
    await env["inbound"].handle(_frame(await _make_envelope(env, space_seed=k1_seed)))
    assert refresher.calls == ["sp-1"]
    assert await env["post_repo"].get("post-1") is None


# ─── v_49: a relayed item's writer cert is verified when present ────────


async def _cert(env, **over):
    kw = dict(
        space_seed=env["space_kp"].private_key,
        space_id="sp-1",
        epoch=await env["crypto"].get_current_epoch("sp-1"),
        instance_pk=env["author_kp"].public_key,
        scope="write",
    )
    kw.update(over)
    return sign_writer_cert(**kw).to_wire()


async def test_relayed_post_with_a_valid_writer_cert_is_accepted(env):
    envelope = await _make_envelope(env, writer_cert=await _cert(env))
    await env["inbound"].handle(_frame(envelope))
    assert await env["post_repo"].get("post-1") is not None


@pytest.mark.parametrize(
    "case", ["forged", "old_epoch", "other_space", "other_household", "comment"]
)
async def test_relayed_post_with_a_bad_writer_cert_is_dropped(env, case, caplog):
    over: dict = {
        "forged": {"space_seed": generate_space_keypair().private_key},
        "old_epoch": {"epoch": 0},
        "other_space": {"space_id": "sp-2"},
        "other_household": {"instance_pk": generate_identity_keypair().public_key},
        "comment": {"scope": "comment"},
    }[case]
    if case == "old_epoch":
        await env["crypto"].rotate_epoch("sp-1")
    envelope = await _make_envelope(env, writer_cert=await _cert(env, **over))
    with caplog.at_level(logging.WARNING):
        await env["inbound"].handle(_frame(envelope))
    assert await env["post_repo"].get("post-1") is None
    assert env["events"] == []
    assert "writer cert" in caplog.text


async def test_relayed_post_with_a_malformed_writer_cert_is_dropped(env, caplog):
    envelope = await _make_envelope(env, writer_cert={"cert_suite": "ed25519"})
    with caplog.at_level(logging.WARNING):
        await env["inbound"].handle(_frame(envelope))
    assert await env["post_repo"].get("post-1") is None


async def test_relayed_post_without_a_cert_keeps_todays_path(env):
    """Pre-v49 author: no cert → accepted on the authority signature."""
    envelope = await _make_envelope(env)
    await env["inbound"].handle(_frame(envelope))
    assert await env["post_repo"].get("post-1") is not None
