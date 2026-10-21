"""Tests for :class:`SpaceSubscriberKeyInbound` (Phase 5b-b consumer).

A GFS subscriber receives the relayed ``space_subscriber_key_handoff``,
unseals the per-space content key with its key-wrap private key, verifies
the space-authority signature against its locally-mirrored space pubkey,
and imports the key — after which it can decrypt the Phase-5a relay it
previously couldn't.

Security invariants under test:

* the current (untargeted) shape is gated by the SEAL alone — sealed to us →
  imported, sealed to another household → dropped quietly at DEBUG (the GFS
  fans the handoff to ALL subscribers, so every household sees the others');
* the LEGACY targeted shape from an older seed-holder is still accepted, and
  a ``target_instance_id`` ≠ us still drops before any unseal;
* a forged authority signature → dropped, NO import (the relay/GFS is never
  trusted — re-verify against the local pinned pubkey);
* sealed to a DIFFERENT key-wrap key (we can't open) → InvalidTag → dropped
  gracefully;
* idempotent double-delivery → exactly one import.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_AUTHOR_PROFILES_BUNDLE,
    AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
    sign_authority_event,
    strip_authority_sig_fields,
)
from socialhome.crypto import (
    generate_space_keypair,
    generate_x25519_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.domain.space_item import pad_json_object
from socialhome.federation.keywrap_seal import seal_to_keywrap
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_key_repo import SqliteSpaceKeyRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.space_crypto_service import (
    KEY_SUITE_AESGCM_256,
    SpaceContentEncryption,
)
from socialhome.services.space_subscriber_key_inbound import (
    SpaceSubscriberKeyInbound,
)


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    own_iid = "subscriber.home"
    kek = KeyManager.from_data_dir(tmp_dir)
    space_repo = SqliteSpaceRepo(db, key_manager=kek)
    key_repo = SqliteSpaceKeyRepo(db)
    crypto = SpaceContentEncryption(key_repo, kek, own_instance_id=own_iid)
    # Our key-wrap keypair (this subscriber's static recipient key).
    kw_kp = generate_x25519_keypair()
    # The space's authority keypair (the host signs handoffs with the seed; we
    # mirror only the public half locally and verify against it).
    skp = generate_space_keypair()

    async def _mirror_space(space_id, *, pubkey_hex):
        # A subscriber mirrors the space row WITHOUT the seed (it never holds
        # the host's private key).
        await space_repo.save(
            Space(
                id=space_id,
                name="S",
                owner_instance_id="host.home",
                owner_username="hostuser",
                identity_public_key=pubkey_hex,
                config_sequence=0,
                features=SpaceFeatures(),
                space_type=SpaceType.PUBLIC,
                join_mode=JoinMode.OPEN,
            )
        )

    svc = SpaceSubscriberKeyInbound(
        space_repo=space_repo,
        space_crypto=crypto,
    )
    svc.attach_identity(
        own_instance_id=own_iid,
        keywrap_private_key=kw_kp.private_key,
    )
    yield {
        "db": db,
        "crypto": crypto,
        "space_repo": space_repo,
        "mirror_space": _mirror_space,
        "svc": svc,
        "own_iid": own_iid,
        "kw_kp": kw_kp,
        "skp": skp,
    }
    await db.shutdown()


def _content_key_meta(epoch, raw_key):
    return {
        "space_content_key": {
            "epoch": epoch,
            "key_suite": KEY_SUITE_AESGCM_256,
            "key_base64": base64.b64encode(raw_key).decode("ascii"),
            "rotated_by": "host.home",
        }
    }


def _handoff_frame(space_id, *, target, sealed, space_seed, from_instance="host.home"):
    """Build a handoff frame. ``target=None`` omits ``target_instance_id``
    entirely — the identity-free shape, where the seal is the only gate."""
    envelope: dict = {"space_id": space_id, "sealed": sealed}
    if target is not None:
        envelope = {
            "space_id": space_id,
            "target_instance_id": target,
            "sealed": sealed,
        }
    envelope.update(
        sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
            space_id=space_id,
            payload=strip_authority_sig_fields(envelope),
            space_seed=space_seed,
        )
    )
    frame = {
        "type": "relay",
        "event_type": AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
        "space_id": space_id,
        "payload": envelope,
    }
    if from_instance is not None:
        frame["from_instance"] = from_instance
    return frame


async def test_valid_handoff_imports_key_and_enables_decrypt(env):
    space_id = "sp-in"
    await env["mirror_space"](space_id, pubkey_hex=env["skp"].public_key.hex())
    # The 32-byte content key the host minted (raw — what export_current_key
    # would yield on the host side). Build a Phase-5a-style ciphertext under
    # it so we can prove decrypt works AFTER the import.
    raw_key = bytes(range(32))
    epoch = 0
    meta = _content_key_meta(epoch, raw_key)
    sealed = seal_to_keywrap(
        recipient_keywrap_pub=env["kw_kp"].public_key,
        plaintext=json.dumps(meta).encode("utf-8"),
    )
    frame = _handoff_frame(
        space_id,
        target=env["own_iid"],
        sealed=sealed,
        space_seed=env["skp"].private_key,
    )

    # Before import: no key, decrypt impossible.
    assert await env["crypto"].export_current_key(space_id) is None

    await env["svc"].handle(frame)

    # After import: the key landed at the stated epoch.
    got = await env["crypto"].export_current_key(space_id)
    assert got is not None
    assert got[0] == epoch
    assert got[1] == raw_key
    # And a ciphertext encrypted under the same key now decrypts.
    _e, ct = await env["crypto"].encrypt(space_id, b"phase-5a-relayed-post")
    pt = await env["crypto"].decrypt(space_id, epoch, ct)
    assert pt == b"phase-5a-relayed-post"


async def test_not_target_dropped(env):
    space_id = "sp-nottarget"
    await env["mirror_space"](space_id, pubkey_hex=env["skp"].public_key.hex())
    raw_key = bytes(range(32))
    meta = _content_key_meta(0, raw_key)
    sealed = seal_to_keywrap(
        recipient_keywrap_pub=env["kw_kp"].public_key,
        plaintext=json.dumps(meta).encode("utf-8"),
    )
    frame = _handoff_frame(
        space_id,
        target="someone-else.home",  # not us
        sealed=sealed,
        space_seed=env["skp"].private_key,
    )

    await env["svc"].handle(frame)

    assert await env["crypto"].export_current_key(space_id) is None


async def test_forged_authority_sig_dropped(env):
    """A handoff signed by a DIFFERENT (attacker) seed must not import — the
    receiver re-verifies the authority sig against the locally-pinned space
    pubkey and never trusts the relay/GFS."""
    space_id = "sp-forged"
    await env["mirror_space"](space_id, pubkey_hex=env["skp"].public_key.hex())
    raw_key = bytes(range(32))
    meta = _content_key_meta(0, raw_key)
    sealed = seal_to_keywrap(
        recipient_keywrap_pub=env["kw_kp"].public_key,
        plaintext=json.dumps(meta).encode("utf-8"),
    )
    attacker = generate_space_keypair()
    frame = _handoff_frame(
        space_id,
        target=env["own_iid"],
        sealed=sealed,
        space_seed=attacker.private_key,  # NOT the pinned space seed
    )

    await env["svc"].handle(frame)

    assert await env["crypto"].export_current_key(space_id) is None


async def test_sealed_to_other_keywrap_key_dropped(env):
    """Sealed to a key-wrap pubkey we don't hold the private half for →
    InvalidTag on open → dropped gracefully, no import, no crash."""
    space_id = "sp-otherseal"
    await env["mirror_space"](space_id, pubkey_hex=env["skp"].public_key.hex())
    raw_key = bytes(range(32))
    meta = _content_key_meta(0, raw_key)
    other_kw = generate_x25519_keypair()  # not ours
    sealed = seal_to_keywrap(
        recipient_keywrap_pub=other_kw.public_key,
        plaintext=json.dumps(meta).encode("utf-8"),
    )
    frame = _handoff_frame(
        space_id,
        target=env["own_iid"],
        sealed=sealed,
        space_seed=env["skp"].private_key,
    )

    await env["svc"].handle(frame)

    assert await env["crypto"].export_current_key(space_id) is None


async def test_idempotent_double_delivery(env):
    space_id = "sp-idem"
    await env["mirror_space"](space_id, pubkey_hex=env["skp"].public_key.hex())
    raw_key = bytes(range(32))
    epoch = 0
    meta = _content_key_meta(epoch, raw_key)
    sealed = seal_to_keywrap(
        recipient_keywrap_pub=env["kw_kp"].public_key,
        plaintext=json.dumps(meta).encode("utf-8"),
    )
    frame = _handoff_frame(
        space_id,
        target=env["own_iid"],
        sealed=sealed,
        space_seed=env["skp"].private_key,
    )

    await env["svc"].handle(frame)
    await env["svc"].handle(frame)  # second delivery — idempotent

    got = await env["crypto"].export_current_key(space_id)
    assert got is not None
    assert got[1] == raw_key


async def test_unmirrored_space_dropped(env):
    """A handoff for a space we don't mirror locally → dropped (no pubkey to
    verify against)."""
    space_id = "sp-unknown"
    raw_key = bytes(range(32))
    meta = _content_key_meta(0, raw_key)
    sealed = seal_to_keywrap(
        recipient_keywrap_pub=env["kw_kp"].public_key,
        plaintext=json.dumps(meta).encode("utf-8"),
    )
    frame = _handoff_frame(
        space_id,
        target=env["own_iid"],
        sealed=sealed,
        space_seed=env["skp"].private_key,
    )

    await env["svc"].handle(frame)  # must not crash

    assert await env["crypto"].export_current_key(space_id) is None


async def test_non_handoff_frame_ignored(env):
    await env["svc"].handle(
        {"type": "relay", "event_type": "space_post_public", "payload": {}}
    )
    await env["svc"].handle({"type": "relay", "event_type": "other"})


# ── Identity-free fan-out: the seal is the gate ───────────────────────────


async def _sealed_meta(env, *, recipient_pub, epoch=0, raw_key=bytes(range(32))):
    return seal_to_keywrap(
        recipient_keywrap_pub=recipient_pub,
        plaintext=json.dumps(_content_key_meta(epoch, raw_key)).encode("utf-8"),
    )


async def test_targeted_handoff_for_us_still_imports(env):
    """LEGACY sender shape: ``target_instance_id`` present and equal to us →
    still imported, so a pre-untargeted seed-holder can still onboard us."""
    space_id = "sp-targeted-us"
    await env["mirror_space"](space_id, pubkey_hex=env["skp"].public_key.hex())
    sealed = await _sealed_meta(env, recipient_pub=env["kw_kp"].public_key)
    await env["svc"].handle(
        _handoff_frame(
            space_id,
            target=env["own_iid"],
            sealed=sealed,
            space_seed=env["skp"].private_key,
        )
    )
    got = await env["crypto"].export_current_key(space_id)
    assert got is not None
    assert got[1] == bytes(range(32))


async def test_targeted_handoff_for_someone_else_still_dropped(env):
    """LEGACY sender shape: ``target_instance_id`` present and NOT us → the
    explicit gate still short-circuits before any unseal."""
    space_id = "sp-targeted-other"
    await env["mirror_space"](space_id, pubkey_hex=env["skp"].public_key.hex())
    # Sealed to US — so only the target gate can be what drops it.
    sealed = await _sealed_meta(env, recipient_pub=env["kw_kp"].public_key)
    await env["svc"].handle(
        _handoff_frame(
            space_id,
            target="someone-else.home",
            sealed=sealed,
            space_seed=env["skp"].private_key,
        )
    )
    assert await env["crypto"].export_current_key(space_id) is None


async def test_untargeted_handoff_sealed_to_us_imports(env):
    """The CURRENT sender shape: no ``target_instance_id`` on the wire
    (nothing identifies the recipient to the GFS) → the unseal itself is the
    gate; sealed to us → imported."""
    space_id = "sp-untargeted-us"
    await env["mirror_space"](space_id, pubkey_hex=env["skp"].public_key.hex())
    sealed = await _sealed_meta(env, recipient_pub=env["kw_kp"].public_key)
    frame = _handoff_frame(
        space_id,
        target=None,
        sealed=sealed,
        space_seed=env["skp"].private_key,
        from_instance=None,
    )
    assert set(frame) == {"type", "event_type", "space_id", "payload"}
    assert "target_instance_id" not in frame["payload"]

    await env["svc"].handle(frame)

    got = await env["crypto"].export_current_key(space_id)
    assert got is not None
    assert got[1] == bytes(range(32))


async def test_untargeted_handoff_for_someone_else_dropped_quietly(env, caplog):
    """A handoff sealed to ANOTHER household (including one we produced
    ourselves) reaches every subscriber. With no target field the unseal
    failure is the "not for us" signal — drop at DEBUG, never WARNING: in a
    space with N subscribers every household sees N-1 of these."""
    caplog.set_level(logging.DEBUG, logger="socialhome")
    space_id = "sp-untargeted-other"
    await env["mirror_space"](space_id, pubkey_hex=env["skp"].public_key.hex())
    other_kw = generate_x25519_keypair()  # not ours
    sealed = await _sealed_meta(env, recipient_pub=other_kw.public_key)

    await env["svc"].handle(
        _handoff_frame(
            space_id,
            target=None,
            sealed=sealed,
            space_seed=env["skp"].private_key,
            from_instance=None,
        )
    )

    assert await env["crypto"].export_current_key(space_id) is None
    noisy = [
        r
        for r in caplog.records
        if r.name.startswith("socialhome") and r.levelno > logging.DEBUG
    ]
    assert noisy == [], [(r.levelname, r.getMessage()) for r in noisy]


# ── v_44: lazy pin heal on an authority failure ──────────────────────────


async def test_handoff_signed_by_a_rotated_key_heals_then_imports(env):
    from socialhome.crypto import generate_space_keypair

    space_id = "sp-heal"
    await env["mirror_space"](space_id, pubkey_hex=env["skp"].public_key.hex())
    k2 = generate_space_keypair()
    calls: list[str] = []
    space_repo = env["space_repo"]

    class _Refresher:
        async def refresh_authority_pin(self, sid: str) -> bool:
            calls.append(sid)
            await space_repo.adopt_authority_key(sid, k2.public_key.hex(), 1)
            return True

    env["svc"].attach_pin_refresher(_Refresher())
    sealed = await _sealed_meta(env, recipient_pub=env["kw_kp"].public_key)
    await env["svc"].handle(
        _handoff_frame(space_id, target=None, sealed=sealed, space_seed=k2.private_key)
    )
    assert calls == [space_id]
    got = await env["crypto"].export_current_key(space_id)
    assert got is not None and got[1] == bytes(range(32))


async def test_old_key_handoff_refused_after_rotation(env):
    space_id = "sp-old"
    await env["mirror_space"](space_id, pubkey_hex=env["skp"].public_key.hex())
    from socialhome.crypto import generate_space_keypair

    await env["space_repo"].adopt_authority_key(
        space_id, generate_space_keypair().public_key.hex(), 1
    )

    class _NoHeal:
        async def refresh_authority_pin(self, sid: str) -> bool:
            return False

    env["svc"].attach_pin_refresher(_NoHeal())
    sealed = await _sealed_meta(env, recipient_pub=env["kw_kp"].public_key)
    await env["svc"].handle(
        _handoff_frame(
            space_id, target=None, sealed=sealed, space_seed=env["skp"].private_key
        )
    )
    assert await env["crypto"].export_current_key(space_id) is None


# ─── v_61 — a new follower's profiles bundle ─────────────────────────────


class _Sink:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def apply_bundled_profiles(self, space_id, *, items, pictures):
        self.calls.append((space_id, items, pictures))
        return len(items) + len(pictures)


def _bundle_frame(
    env, space_id, *, plaintext: bytes, seed=None, kw_pub=None, issued_at=None
):
    sealed = seal_to_keywrap(
        recipient_keywrap_pub=kw_pub or env["kw_kp"].public_key, plaintext=plaintext
    )
    envelope: dict = {
        "space_id": space_id,
        "sealed": sealed,
        "issued_at": issued_at or datetime.now(UTC).isoformat(),
    }
    envelope.update(
        sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_AUTHOR_PROFILES_BUNDLE,
            space_id=space_id,
            payload=strip_authority_sig_fields(envelope),
            space_seed=seed or env["skp"].private_key,
        )
    )
    return {
        "type": "relay",
        "event_type": AUTHORITY_EVENT_SPACE_AUTHOR_PROFILES_BUNDLE,
        "space_id": space_id,
        "payload": envelope,
    }


def _bundle_plain(space_id="sp-b", epoch=2, items=None, pictures=()) -> bytes:
    return pad_json_object(
        {
            "space_id": space_id,
            "part": 0,
            "parts": 1,
            "items": [
                {"epoch": epoch, "item": i}
                for i in (
                    items if items is not None else [{"item_type": "author_profile"}]
                )
            ],
            "pictures": [
                {"author": a, "hash": h, "picture": pic} for a, h, pic in pictures
            ],
        }
    )


async def test_a_bundle_sealed_to_us_goes_to_the_item_checks(env):
    await env["mirror_space"]("sp-b", pubkey_hex=env["skp"].public_key.hex())
    sink = _Sink()
    env["svc"].attach_profiles_sink(sink)
    await env["svc"].handle(_bundle_frame(env, "sp-b", plaintext=_bundle_plain()))
    assert sink.calls == [("sp-b", [(2, {"item_type": "author_profile"})], [])]


async def test_a_picture_part_goes_to_the_picture_checks(env):
    await env["mirror_space"]("sp-b", pubkey_hex=env["skp"].public_key.hex())
    sink = _Sink()
    env["svc"].attach_profiles_sink(sink)
    plain = _bundle_plain(items=[], pictures=[("uid-a", "0123456789abcdef", "UklG")])
    await env["svc"].handle(_bundle_frame(env, "sp-b", plaintext=plain))
    assert sink.calls == [("sp-b", [], [("uid-a", "0123456789abcdef", "UklG")])]


async def test_a_bundle_with_a_forged_authority_signature_is_dropped(env, caplog):
    await env["mirror_space"]("sp-b", pubkey_hex=env["skp"].public_key.hex())
    sink = _Sink()
    env["svc"].attach_profiles_sink(sink)
    forged = generate_space_keypair().private_key
    with caplog.at_level(logging.WARNING):
        await env["svc"].handle(
            _bundle_frame(env, "sp-b", plaintext=_bundle_plain(), seed=forged)
        )
    assert sink.calls == []
    assert "authority signature failed" in caplog.text


async def test_a_bundle_sealed_to_someone_else_is_dropped_quietly(env, caplog):
    await env["mirror_space"]("sp-b", pubkey_hex=env["skp"].public_key.hex())
    sink = _Sink()
    env["svc"].attach_profiles_sink(sink)
    other = generate_x25519_keypair()
    with caplog.at_level(logging.INFO):
        await env["svc"].handle(
            _bundle_frame(
                env, "sp-b", plaintext=_bundle_plain(), kw_pub=other.public_key
            )
        )
    assert sink.calls == []
    assert "space_subscriber_key.inbound" not in caplog.text


async def test_a_bundle_naming_another_space_inside_is_dropped(env):
    await env["mirror_space"]("sp-b", pubkey_hex=env["skp"].public_key.hex())
    sink = _Sink()
    env["svc"].attach_profiles_sink(sink)
    await env["svc"].handle(
        _bundle_frame(env, "sp-b", plaintext=_bundle_plain(space_id="sp-other"))
    )
    await env["svc"].handle(_bundle_frame(env, "sp-b", plaintext=b"not a bundle"))
    assert sink.calls == []


async def test_a_bundle_for_an_unknown_space_or_without_a_sink_is_dropped(env):
    sink = _Sink()
    await env["svc"].handle(_bundle_frame(env, "sp-b", plaintext=_bundle_plain()))
    env["svc"].attach_profiles_sink(sink)
    await env["svc"].handle(_bundle_frame(env, "sp-b", plaintext=_bundle_plain()))
    await env["svc"].handle({"event_type": "space_author_profiles_bundle"})
    assert sink.calls == []


@pytest.mark.parametrize(
    "skew_s", [-7200, 3600, None], ids=["old", "future", "undated"]
)
async def test_a_stale_or_undated_bundle_is_refused(env, skew_s):
    """Review minor: a GFS replaying an old bundle (or one from the future)
    gets nothing applied."""
    issued_at = (
        (datetime.now(UTC) + timedelta(seconds=skew_s)).isoformat()
        if skew_s is not None
        else "not a time"
    )
    await env["mirror_space"]("sp-b", pubkey_hex=env["skp"].public_key.hex())
    sink = _Sink()
    env["svc"].attach_profiles_sink(sink)
    await env["svc"].handle(
        _bundle_frame(env, "sp-b", plaintext=_bundle_plain(), issued_at=issued_at)
    )
    assert sink.calls == []
