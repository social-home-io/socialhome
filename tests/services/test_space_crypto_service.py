"""Tests for SpaceContentEncryption + space crypto helpers (§4.3)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from socialhome.crypto import (
    derive_space_id,
    generate_space_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.federation.multi_recipient import derive_mr_key
from socialhome.infrastructure.key_manager import KeyManager, KeyManagerError
from socialhome.repositories.space_key_repo import (
    SqliteSpaceKeyRepo,
)
from socialhome.services import space_crypto_service as scs
from socialhome.services.space_crypto_service import (
    AUTHORITY_SIG_SUITE_ED25519,
    SUPPORTED_AUTHORITY_SIG_SUITES,
    SpaceContentEncryption,
    UnsupportedAuthoritySuite,
    authority_signing_bytes,
    create_space_identity,
    sign_authority_event,
    sign_space_config,
    verify_authority_event,
    verify_space_config,
)
from socialhome.space_media_seal import derive_space_media_key

# ─── Crypto helpers ──────────────────────────────────────────────────────


def test_derive_space_id_from_public_key():
    kp = generate_space_keypair()
    sid = derive_space_id(kp.public_key)
    assert isinstance(sid, str)
    assert len(sid) == 32
    # Same key → same id (deterministic).
    assert derive_space_id(kp.public_key) == sid


def test_derive_space_id_rejects_wrong_size():
    with pytest.raises(ValueError):
        derive_space_id(b"too-short")


def test_create_space_identity_returns_consistent_triple():
    seed, pk, sid = create_space_identity()
    assert len(seed) == 32 and len(pk) == 32
    assert sid == derive_space_id(pk)


def test_sign_and_verify_space_config_roundtrip():
    seed, pk, _sid = create_space_identity()
    payload = b'{"event":"rename","new_name":"Vacation"}'
    sig = sign_space_config(payload, space_seed=seed)
    assert verify_space_config(payload, sig, space_public_key=pk) is True


def test_verify_space_config_rejects_tampering():
    seed, pk, _ = create_space_identity()
    payload = b'{"event":"rename","new_name":"Vacation"}'
    sig = sign_space_config(payload, space_seed=seed)
    # Modified payload — signature should fail.
    assert verify_space_config(b'{"event":"hijack"}', sig, space_public_key=pk) is False


def test_verify_space_config_rejects_garbage_signature():
    _, pk, _ = create_space_identity()
    assert verify_space_config(b"x", "not-base64-!!!", space_public_key=pk) is False


# ─── SpaceContentEncryption ──────────────────────────────────────────────


@pytest.fixture
async def crypto_env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    # space_id is FK-enforced, so create a parent spaces row first.
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-1', 'Test', 'inst-1', 'pascal', ?)",
        ("ab" * 32,),
    )
    repo = SqliteSpaceKeyRepo(db)
    kek = KeyManager.from_data_dir(tmp_dir)
    crypto = SpaceContentEncryption(repo, kek)
    yield crypto, repo
    await db.shutdown()


async def test_export_current_key_returns_unwrapped_bytes_and_epoch(crypto_env):
    """§D1b #117 — the host hands a brand-new remote member the
    space content key inside the (already-encrypted) invite envelope
    so they can decrypt subsequent SPACE_POST_CREATED events. The
    returned bytes are the *unwrapped* AES-256 key — the federation
    layer is responsible for the secrecy of the envelope they go in."""
    crypto, _ = crypto_env
    await crypto.initialise_for_space("sp-1")
    result = await crypto.export_current_key("sp-1")
    assert result is not None
    epoch, raw = result
    assert epoch == 0
    assert isinstance(raw, bytes)
    assert len(raw) == 32  # AES-256


async def test_rotate_epoch_min_epoch_lifts_the_new_epoch(crypto_env):
    """v_44 post-restore: the new content epoch starts at ``min_epoch`` when
    that is above the next one, and is never lowered by it."""
    crypto, _ = crypto_env
    await crypto.initialise_for_space("sp-1")
    assert await crypto.rotate_epoch("sp-1", min_epoch=1000) == 1000
    assert await crypto.rotate_epoch("sp-1", min_epoch=5) == 1001
    assert await crypto.get_current_epoch("sp-1") == 1001


async def test_export_current_key_returns_none_when_uninitialised(crypto_env):
    """A space with no epoch key yet exports ``None`` — caller
    skips shipping the field rather than synthesising garbage."""
    crypto, _ = crypto_env
    # sp-1 exists as a parent row from the fixture but has no
    # epoch key until ``initialise_for_space`` runs.
    assert await crypto.export_current_key("sp-1") is None


async def test_import_key_then_decrypt_smoke(crypto_env):
    """End-to-end of the §D1b key handoff: encrypt → export the
    raw key → import (simulating receiver-side persistence) →
    decrypt under the imported key. The at-rest KEK wrap is
    re-applied on import so the row matches the local invariant."""
    crypto, _ = crypto_env
    await crypto.initialise_for_space("sp-1")
    epoch, ct = await crypto.encrypt("sp-1", b"hello space")
    exported = await crypto.export_current_key("sp-1")
    assert exported is not None
    _epoch, raw_key = exported
    # ``import_key`` upserts under the same (space_id, epoch), so
    # we can re-decrypt — proves the same bytes decrypt after the
    # round-trip through unwrap → re-wrap.
    await crypto.import_key("sp-1", epoch, raw_key)
    plaintext = await crypto.decrypt("sp-1", epoch, ct)
    assert plaintext == b"hello space"


async def test_import_key_rejects_wrong_length(crypto_env):
    crypto, _ = crypto_env
    with pytest.raises(ValueError):
        await crypto.import_key("sp-1", 0, b"too short")


# ─── Concurrent-rekey collision safety (Phase 4b) ────────────────────────


async def test_rotate_epoch_records_own_instance_as_rotated_by(tmp_dir):
    """A locally-minted rotation stamps THIS household's instance id on the
    new epoch row so concurrent rotations have a deterministic tiebreak."""
    db = AsyncDatabase(tmp_dir / "rb.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-rb', 'T', 'inst-x', 'p', ?)",
        ("cd" * 32,),
    )
    repo = SqliteSpaceKeyRepo(db)
    kek = KeyManager.from_data_dir(tmp_dir)
    crypto = SpaceContentEncryption(repo, kek, own_instance_id="inst-AAA")
    epoch = await crypto.rotate_epoch("sp-rb")
    row = await repo.get("sp-rb", epoch)
    assert row.rotated_by == "inst-AAA"
    await db.shutdown()


async def test_import_key_collision_smaller_rotated_by_wins_both_orders(
    crypto_env,
):
    """Two admins rotate to the SAME epoch with DIFFERENT keys. Every receiver
    must converge to the lexicographically-smallest ``rotated_by``'s key,
    regardless of arrival order — so the space doesn't diverge."""
    crypto, _ = crypto_env
    key_lo = b"L" * 32  # from instance "aaa" (smaller)
    key_hi = b"H" * 32  # from instance "zzz" (larger)

    # Order 1: high first, then low → low must win.
    await crypto.import_key("sp-1", 5, key_hi, rotated_by="zzz")
    await crypto.import_key("sp-1", 5, key_lo, rotated_by="aaa")
    assert (await crypto.export_current_key("sp-1")) == (5, key_lo)


async def test_import_key_collision_low_then_high_keeps_low(crypto_env):
    """Reverse arrival order converges to the same winner — low arrives
    first, the later high-``rotated_by`` import must NOT clobber it."""
    crypto, _ = crypto_env
    key_lo = b"L" * 32
    key_hi = b"H" * 32
    await crypto.import_key("sp-1", 5, key_lo, rotated_by="aaa")
    await crypto.import_key("sp-1", 5, key_hi, rotated_by="zzz")
    assert (await crypto.export_current_key("sp-1")) == (5, key_lo)


async def test_import_key_null_rotated_by_does_not_clobber_existing(crypto_env):
    """Back-compat: an incoming key with NO ``rotated_by`` (older peer) must
    NOT overwrite an existing row that DOES carry one — otherwise an older
    peer's blind upsert would re-introduce the divergence this fixes."""
    crypto, _ = crypto_env
    key_known = b"K" * 32
    key_anon = b"A" * 32
    await crypto.import_key("sp-1", 5, key_known, rotated_by="aaa")
    await crypto.import_key("sp-1", 5, key_anon, rotated_by=None)
    assert (await crypto.export_current_key("sp-1")) == (5, key_known)


async def test_import_key_null_rotated_by_applies_when_existing_also_null(
    crypto_env,
):
    """When neither side carries ``rotated_by`` the behaviour degrades to
    today's last-writer-wins (the pre-Phase-4b contract for legacy peers)."""
    crypto, _ = crypto_env
    await crypto.import_key("sp-1", 5, b"1" * 32, rotated_by=None)
    await crypto.import_key("sp-1", 5, b"2" * 32, rotated_by=None)
    assert (await crypto.export_current_key("sp-1")) == (5, b"2" * 32)


async def test_apply_space_content_key_rejects_unknown_suite(crypto_env):
    """Forward-compat — receivers MUST reject suites they don't know
    rather than fall back to a default. Mirrors the kem_suite
    rejection in routed_crypto.py so the wire shape is safe to grow
    without breaking older receivers (#117)."""
    from socialhome.services.space_crypto_service import UnsupportedKeySuite
    from socialhome.services.space_service import (
        apply_space_content_key_from_metadata,
    )

    crypto, _ = crypto_env
    bad_meta = {
        "space_content_key": {
            "epoch": 0,
            "key_suite": "x25519+mlkem768-prophet-2030",
            "key_base64": "AAAA",
        },
    }
    with pytest.raises(UnsupportedKeySuite):
        await apply_space_content_key_from_metadata(
            "sp-1",
            meta=bad_meta,
            space_crypto_service=crypto,
        )


async def test_apply_space_content_key_accepts_default_suite_when_missing(
    crypto_env,
):
    """Older sender that doesn't include ``key_suite`` (first
    revision of this protocol) defaults to ``aesgcm-256`` — the
    only suite supported today — so the key still lands."""
    import base64

    from socialhome.services.space_service import (
        apply_space_content_key_from_metadata,
    )

    crypto, _ = crypto_env
    raw = b"x" * 32
    meta = {
        "space_content_key": {
            "epoch": 7,
            # No ``key_suite`` — older sender.
            "key_base64": base64.b64encode(raw).decode("ascii"),
        },
    }
    await apply_space_content_key_from_metadata(
        "sp-1",
        meta=meta,
        space_crypto_service=crypto,
    )
    exported = await crypto.export_current_key("sp-1")
    assert exported is not None
    assert exported == (7, raw)


async def test_initialise_for_space_creates_epoch_zero(crypto_env):
    crypto, _ = crypto_env
    epoch = await crypto.initialise_for_space("sp-1")
    assert epoch == 0


async def test_initialise_is_idempotent(crypto_env):
    crypto, _ = crypto_env
    e1 = await crypto.initialise_for_space("sp-1")
    e2 = await crypto.initialise_for_space("sp-1")
    assert e1 == e2 == 0


async def test_rotate_epoch_increments_version(crypto_env):
    crypto, _ = crypto_env
    await crypto.initialise_for_space("sp-1")
    new_epoch = await crypto.rotate_epoch("sp-1")
    assert new_epoch == 1
    # Subsequent rotation also increments.
    assert await crypto.rotate_epoch("sp-1") == 2


async def test_encrypt_decrypt_roundtrip(crypto_env):
    crypto, _ = crypto_env
    await crypto.initialise_for_space("sp-1")
    epoch, ct = await crypto.encrypt("sp-1", b"the-payload-bytes")
    assert epoch == 0
    plaintext = await crypto.decrypt("sp-1", epoch, ct)
    assert plaintext == b"the-payload-bytes"


async def test_encrypt_without_init_raises_per_encryption_first_rule(crypto_env):
    """CLAUDE.md: never silently fall back to plaintext."""
    crypto, _ = crypto_env
    with pytest.raises(RuntimeError):
        await crypto.encrypt("sp-1", b"data")


async def test_decrypt_with_unknown_epoch_raises(crypto_env):
    crypto, _ = crypto_env
    await crypto.initialise_for_space("sp-1")
    _, ct = await crypto.encrypt("sp-1", b"data")
    with pytest.raises(RuntimeError):
        await crypto.decrypt("sp-1", epoch=99, ciphertext=ct)


async def test_decrypt_old_epoch_still_works_after_rotation(crypto_env):
    """Old epoch keys are kept indefinitely so historical content stays readable."""
    crypto, _ = crypto_env
    await crypto.initialise_for_space("sp-1")
    epoch0, ct0 = await crypto.encrypt("sp-1", b"epoch-0-content")
    await crypto.rotate_epoch("sp-1")
    epoch1, ct1 = await crypto.encrypt("sp-1", b"epoch-1-content")
    assert epoch0 == 0 and epoch1 == 1
    # Both old and new content decrypt correctly.
    assert await crypto.decrypt("sp-1", 0, ct0) == b"epoch-0-content"
    assert await crypto.decrypt("sp-1", 1, ct1) == b"epoch-1-content"


async def test_decrypt_rejects_malformed_ciphertext(crypto_env):
    crypto, _ = crypto_env
    await crypto.initialise_for_space("sp-1")
    with pytest.raises(ValueError):
        await crypto.decrypt("sp-1", 0, "no-colon-in-here")


async def test_get_current_epoch_when_uninitialised(crypto_env):
    crypto, _ = crypto_env
    assert await crypto.get_current_epoch("sp-1") is None


async def test_get_current_epoch_returns_latest(crypto_env):
    crypto, _ = crypto_env
    await crypto.initialise_for_space("sp-1")
    await crypto.rotate_epoch("sp-1")
    assert await crypto.get_current_epoch("sp-1") == 1


# ─── Sync chunks (§25.6 direct space sync) ────────────────────────────────


async def test_encrypt_decrypt_chunk_roundtrip(crypto_env):
    """A sync chunk AAD-binds to ``space_id:epoch:sync_id`` so it can
    only be decrypted with the matching tuple."""
    crypto, _ = crypto_env
    await crypto.initialise_for_space("sp-1")
    epoch, ciphertext = await crypto.encrypt_chunk(
        space_id="sp-1",
        sync_id="sync-abc",
        plaintext=b"chunk-bytes",
    )
    assert epoch == 0
    plain = await crypto.decrypt_chunk(
        space_id="sp-1",
        epoch=epoch,
        sync_id="sync-abc",
        ciphertext=ciphertext,
    )
    assert plain == b"chunk-bytes"


async def test_encrypt_chunk_without_init_raises(crypto_env):
    """Per the encryption-first rule, encrypt_chunk must NOT silently
    fall back to plaintext when no key has been minted."""
    crypto, _ = crypto_env
    with pytest.raises(RuntimeError, match="no key for space"):
        await crypto.encrypt_chunk(
            space_id="missing",
            sync_id="sync-1",
            plaintext=b"x",
        )


async def test_decrypt_chunk_unknown_epoch_raises(crypto_env):
    crypto, _ = crypto_env
    await crypto.initialise_for_space("sp-1")
    with pytest.raises(RuntimeError, match="missing epoch"):
        await crypto.decrypt_chunk(
            space_id="sp-1",
            epoch=99,
            sync_id="sync-1",
            ciphertext="aa:bb",
        )


async def test_decrypt_chunk_rejects_malformed_ciphertext(crypto_env):
    crypto, _ = crypto_env
    await crypto.initialise_for_space("sp-1")
    with pytest.raises(ValueError, match="Malformed space ciphertext"):
        await crypto.decrypt_chunk(
            space_id="sp-1",
            epoch=0,
            sync_id="sync-1",
            ciphertext="no-colon",
        )


async def test_decrypt_chunk_rejects_wrong_sync_id(crypto_env):
    """A chunk's AAD includes ``sync_id`` — replaying a chunk under a
    different sync_id must fail the tag check (§25.8.18)."""
    from cryptography.exceptions import InvalidTag

    crypto, _ = crypto_env
    await crypto.initialise_for_space("sp-1")
    epoch, ciphertext = await crypto.encrypt_chunk(
        space_id="sp-1",
        sync_id="sync-original",
        plaintext=b"hi",
    )
    with pytest.raises(InvalidTag):
        await crypto.decrypt_chunk(
            space_id="sp-1",
            epoch=epoch,
            sync_id="sync-different",
            ciphertext=ciphertext,
        )


# ─── verify_space_config error paths ──────────────────────────────────────


def test_verify_space_config_returns_false_for_bad_base64():
    """Malformed base64 signature is rejected without raising — caller
    just sees False so the §24.11 inbound pipeline can drop the event."""
    _, pk, _ = create_space_identity()
    assert (
        verify_space_config(
            b"payload",
            "not-valid-base64-!@#",
            space_public_key=pk,
        )
        is False
    )


# ─── Space-authority signature primitives ─────────────────────────────────


def _authority_args():
    return {
        "event_type": "SPACE_MEMBER_GOSSIP",
        "space_id": "sp-abc",
        "payload": {"user_id": "u1", "role": "member", "member_version": 3},
    }


def test_authority_sig_suite_constant_and_set():
    assert AUTHORITY_SIG_SUITE_ED25519 == "ed25519"
    assert AUTHORITY_SIG_SUITE_ED25519 in SUPPORTED_AUTHORITY_SIG_SUITES


def test_strip_authority_sig_fields_removes_only_the_two_sig_keys():
    """The strip helper returns a copy without the two signature fields and
    leaves the original payload untouched (used identically on sign + verify
    sides so canonical bytes match)."""
    from socialhome.services.space_crypto_service import strip_authority_sig_fields

    payload = {
        "space_id": "sp-abc",
        "user_id": "u1",
        "role": "member",
        "member_version": 3,
        "authority_sig": "AAA",
        "authority_sig_suite": "ed25519",
    }
    stripped = strip_authority_sig_fields(payload)
    assert stripped == {
        "space_id": "sp-abc",
        "user_id": "u1",
        "role": "member",
        "member_version": 3,
    }
    # A copy — the original keeps its sig fields.
    assert "authority_sig" in payload
    assert stripped is not payload


def test_strip_authority_sig_fields_noop_when_absent():
    """Stripping a payload with no sig fields returns an equal copy."""
    from socialhome.services.space_crypto_service import strip_authority_sig_fields

    payload = {"space_id": "sp", "user_id": "u"}
    stripped = strip_authority_sig_fields(payload)
    assert stripped == payload
    assert stripped is not payload


def test_signed_payload_verifies_after_stripping_the_merged_sig_fields():
    """End-to-end: sign over the bare payload, merge the sig fields in, then
    verify by stripping them back out — the canonical bytes round-trip. This
    is the exact sign/verify symmetry the gossip path depends on."""
    from socialhome.services.space_crypto_service import strip_authority_sig_fields

    seed, pk, _ = create_space_identity()
    bare = {
        "space_id": "sp-abc",
        "user_id": "u1",
        "role": "member",
        "member_version": 4,
    }
    signed = sign_authority_event(
        event_type="SPACE_MEMBER_GOSSIP",
        space_id="sp-abc",
        payload=bare,
        space_seed=seed,
    )
    wire = {**bare, **signed}  # what travels on the wire
    assert (
        verify_authority_event(
            event_type="SPACE_MEMBER_GOSSIP",
            space_id="sp-abc",
            payload=strip_authority_sig_fields(wire),
            authority_sig=wire["authority_sig"],
            authority_sig_suite=wire["authority_sig_suite"],
            space_public_key=pk,
        )
        is True
    )


def test_authority_signing_bytes_is_domain_separated_and_canonical():
    msg = authority_signing_bytes(**_authority_args())
    assert msg.startswith(b"space-authority:v1:")
    # Canonical: field order in the payload dict must not change the bytes.
    reordered = {
        "event_type": "SPACE_MEMBER_GOSSIP",
        "space_id": "sp-abc",
        "payload": {"role": "member", "member_version": 3, "user_id": "u1"},
    }
    assert authority_signing_bytes(**reordered) == msg


def test_sign_and_verify_authority_event_roundtrip():
    seed, pk, _ = create_space_identity()
    args = _authority_args()
    signed = sign_authority_event(space_seed=seed, **args)
    assert signed["authority_sig_suite"] == AUTHORITY_SIG_SUITE_ED25519
    assert isinstance(signed["authority_sig"], str)
    assert (
        verify_authority_event(
            authority_sig=signed["authority_sig"],
            authority_sig_suite=signed["authority_sig_suite"],
            space_public_key=pk,
            **args,
        )
        is True
    )


def test_verify_authority_event_rejects_wrong_pubkey():
    seed, _, _ = create_space_identity()
    _, other_pk, _ = create_space_identity()
    args = _authority_args()
    signed = sign_authority_event(space_seed=seed, **args)
    assert (
        verify_authority_event(
            authority_sig=signed["authority_sig"],
            authority_sig_suite=signed["authority_sig_suite"],
            space_public_key=other_pk,
            **args,
        )
        is False
    )


def test_verify_authority_event_rejects_tampered_payload():
    seed, pk, _ = create_space_identity()
    args = _authority_args()
    signed = sign_authority_event(space_seed=seed, **args)
    tampered = dict(args)
    tampered["payload"] = {**args["payload"], "role": "admin"}
    assert (
        verify_authority_event(
            authority_sig=signed["authority_sig"],
            authority_sig_suite=signed["authority_sig_suite"],
            space_public_key=pk,
            **tampered,
        )
        is False
    )


def test_verify_authority_event_rejects_tampered_event_type():
    seed, pk, _ = create_space_identity()
    args = _authority_args()
    signed = sign_authority_event(space_seed=seed, **args)
    tampered = dict(args)
    tampered["event_type"] = "SPACE_MEMBER_REMOVED"
    assert (
        verify_authority_event(
            authority_sig=signed["authority_sig"],
            authority_sig_suite=signed["authority_sig_suite"],
            space_public_key=pk,
            **tampered,
        )
        is False
    )


def test_verify_authority_event_rejects_tampered_space_id():
    seed, pk, _ = create_space_identity()
    args = _authority_args()
    signed = sign_authority_event(space_seed=seed, **args)
    tampered = dict(args)
    tampered["space_id"] = "sp-other"
    assert (
        verify_authority_event(
            authority_sig=signed["authority_sig"],
            authority_sig_suite=signed["authority_sig_suite"],
            space_public_key=pk,
            **tampered,
        )
        is False
    )


def test_verify_authority_event_raises_on_unknown_suite():
    seed, pk, _ = create_space_identity()
    args = _authority_args()
    signed = sign_authority_event(space_seed=seed, **args)
    with pytest.raises(UnsupportedAuthoritySuite):
        verify_authority_event(
            authority_sig=signed["authority_sig"],
            authority_sig_suite="ed25519+mldsa65",
            space_public_key=pk,
            **args,
        )


def test_verify_authority_event_returns_false_on_malformed_sig():
    _, pk, _ = create_space_identity()
    args = _authority_args()
    assert (
        verify_authority_event(
            authority_sig="not-valid-base64-!@#",
            authority_sig_suite=AUTHORITY_SIG_SUITE_ED25519,
            space_public_key=pk,
            **args,
        )
        is False
    )


async def test_reset_to_key_replaces_the_epoch_and_drops_later_ones(crypto_env):
    """v_44 baseline reset: the owner's rotation bundle wins over whatever
    a demoted seed holder pushed — same epoch regardless of ``rotated_by``,
    and every epoch above it is gone."""
    crypto, repo = crypto_env
    bogus = b"\x01" * 32
    await crypto.import_key("sp-1", 3, bogus, rotated_by="a-smallest")
    await crypto.import_key("sp-1", 1_000_000, bogus, rotated_by="b")
    owner_key = b"\x02" * 32
    await crypto.reset_to_key(
        "sp-1", 3, owner_key, rotated_by="z-owner", authority_epoch=1
    )
    assert await crypto.get_current_epoch("sp-1") == 3
    exported = await crypto.export_current_key("sp-1")
    assert exported == (3, owner_key)
    assert (await repo.get("sp-1", 3)).rotated_by == "z-owner"
    assert await repo.get("sp-1", 1_000_000) is None


async def test_reset_to_key_rejects_wrong_length(crypto_env):
    crypto, _repo = crypto_env
    with pytest.raises(ValueError):
        await crypto.reset_to_key(
            "sp-1", 1, b"short", rotated_by="x", authority_epoch=1
        )


async def test_seal_at_rest_round_trips_only_under_its_context(crypto_env):
    """Migration 0093: a pending invitation's content key is KEK-wrapped at
    rest, bound to that invitation — never plaintext, never movable."""
    crypto, _ = crypto_env
    raw = b"\x07" * 32
    sealed = crypto.seal_at_rest(raw, context="invite:sp-1:t-1")
    assert raw.hex() not in sealed
    assert crypto.open_at_rest(sealed, context="invite:sp-1:t-1") == raw
    with pytest.raises(KeyManagerError):
        crypto.open_at_rest(sealed, context="invite:sp-1:t-2")
    with pytest.raises(ValueError):
        crypto.seal_at_rest(b"short", context="invite:sp-1:t-1")


# ─── Space media key (GFS media cache) ───────────────────────────────────


async def test_media_key_is_derived_per_epoch_never_the_raw_key(crypto_env):
    """``media_key`` hands out only the HKDF-derived media key of an epoch
    (:func:`derive_space_media_key`), never the content key itself."""
    crypto, _ = crypto_env
    assert await crypto.media_key("sp-1") is None
    assert await crypto.media_key("sp-1", epoch=0) is None
    first = await crypto.initialise_for_space("sp-1")
    _epoch, raw = await crypto.export_current_key("sp-1")
    current = await crypto.media_key("sp-1")
    assert current == (first, derive_space_media_key(raw))
    assert current[1] != raw
    second = await crypto.rotate_epoch("sp-1")
    assert (await crypto.media_key("sp-1"))[0] == second
    # An older epoch's key stays reachable — a descriptor names its epoch.
    assert await crypto.media_key("sp-1", epoch=first) == current
    assert await crypto.media_key("sp-1", epoch=999) is None


# ─── v_90 multi-recipient keys ───────────────────────────────────────────


async def _add_space(repo, space_id: str) -> None:
    await repo._db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?, 'T', 'inst-1', 'pascal', ?)",
        (space_id, "ab" * 32),
    )


async def _current_mr(crypto, space_id: str) -> tuple[int, str, bytes]:
    """The current epoch's ``(epoch, kid, key)``, whatever its age."""
    epoch, raw = await crypto.export_current_key(space_id)
    return (epoch, *derive_mr_key(raw))


def _clock_after(monkeypatch, seconds: float) -> None:
    """Move the multi grace clock *seconds* past the real now."""
    real = scs._utcnow()
    monkeypatch.setattr(scs, "_utcnow", lambda: real + timedelta(seconds=seconds))


async def test_mr_key_is_derived_from_the_current_epoch(crypto_env, monkeypatch):
    crypto, _ = crypto_env
    assert await crypto.mr_key("sp-1") is None
    await crypto.initialise_for_space("sp-1")
    await crypto.rotate_epoch("sp-1")
    _clock_after(monkeypatch, scs.MULTI_EPOCH_GRACE_S + 1)
    epoch, raw = await crypto.export_current_key("sp-1")
    got = await crypto.mr_key("sp-1")
    assert got == (epoch, *derive_mr_key(raw))
    # Only derived material leaves.
    assert raw not in got


async def test_no_mr_key_while_the_newest_epoch_is_in_its_grace(
    crypto_env, monkeypatch
):
    """A member still importing the newest epoch would drop a frame sealed
    under it while the GFS reports it delivered — so no multi key for
    MULTI_EPOCH_GRACE_S after a rotation, and never an older epoch's (a
    removed member must not read)."""
    crypto, repo = crypto_env
    await crypto.initialise_for_space("sp-1")
    await repo._db.enqueue(
        "UPDATE space_keys SET created_at='2020-01-01 00:00:00' WHERE space_id='sp-1'"
    )
    assert await crypto.mr_key("sp-1") is not None  # epoch 0 is old
    await crypto.rotate_epoch("sp-1")
    assert await crypto.mr_key("sp-1") is None  # epoch 1 is brand new
    _clock_after(monkeypatch, scs.MULTI_EPOCH_GRACE_S - 5)
    assert await crypto.mr_key("sp-1") is None
    _clock_after(monkeypatch, scs.MULTI_EPOCH_GRACE_S + 5)
    assert await crypto.mr_key("sp-1") == await _current_mr(crypto, "sp-1")
    assert (await crypto.mr_key("sp-1"))[0] == 1


@pytest.mark.parametrize(
    ("stored", "usable"),
    [
        ("2020-01-01 00:00:00", True),  # SQLite datetime('now') — naive UTC
        ("2020-01-01T00:00:00+00:00", True),  # tz-aware ISO
        ("2020-01-01T00:00:00", True),  # naive ISO — UTC
        ("not a time", False),  # unreadable → fail closed
    ],
)
async def test_mr_key_reads_both_stored_time_shapes(crypto_env, stored, usable):
    crypto, repo = crypto_env
    await crypto.initialise_for_space("sp-1")
    await repo._db.enqueue(
        "UPDATE space_keys SET created_at=? WHERE space_id='sp-1'", (stored,)
    )
    assert (await crypto.mr_key("sp-1") is not None) is usable


async def test_mr_key_for_kid_resolves_recent_epochs_of_every_space(crypto_env):
    crypto, repo = crypto_env
    await _add_space(repo, "sp-2")
    await crypto.initialise_for_space("sp-1")
    await crypto.initialise_for_space("sp-2")
    _, kid1, key1 = await _current_mr(crypto, "sp-1")
    _, kid2, key2 = await _current_mr(crypto, "sp-2")
    assert await crypto.mr_key_for_kid(kid1) == ("sp-1", 0, key1)
    assert await crypto.mr_key_for_kid(kid2) == ("sp-2", 0, key2)
    assert await crypto.mr_key_for_kid("nope") is None


async def test_mr_key_for_kid_keeps_only_the_three_newest_epochs(crypto_env):
    crypto, _ = crypto_env
    kids = []
    for _ in range(4):
        await crypto.rotate_epoch("sp-1")
        kids.append((await _current_mr(crypto, "sp-1"))[1])
    assert await crypto.mr_key_for_kid(kids[0]) is None
    for epoch, kid in enumerate(kids[1:], start=1):
        assert (await crypto.mr_key_for_kid(kid))[:2] == ("sp-1", epoch)


async def test_the_kid_cache_follows_rotation_import_and_reset(crypto_env):
    crypto, _ = crypto_env
    await crypto.initialise_for_space("sp-1")
    _, kid0, _ = await _current_mr(crypto, "sp-1")
    assert await crypto.mr_key_for_kid(kid0) is not None  # cache built
    await crypto.rotate_epoch("sp-1")
    _, kid1, _ = await _current_mr(crypto, "sp-1")
    assert (await crypto.mr_key_for_kid(kid1))[:2] == ("sp-1", 1)
    imported = b"\x07" * 32
    await crypto.import_key("sp-1", 2, imported)
    kid2, key2 = derive_mr_key(imported)
    assert await crypto.mr_key_for_kid(kid2) == ("sp-1", 2, key2)
    reset = b"\x08" * 32
    await crypto.reset_to_key("sp-1", 3, reset, rotated_by=None, authority_epoch=0)
    kid3, key3 = derive_mr_key(reset)
    assert await crypto.mr_key_for_kid(kid3) == ("sp-1", 3, key3)


async def test_a_miss_rebuilds_the_cache_at_most_once_a_minute(crypto_env, monkeypatch):
    """A key written behind the service's back is found by the backstop
    rebuild — but a flood of unknown kids costs one rebuild a minute, not
    one query per frame."""
    crypto, repo = crypto_env
    now = [1000.0]
    monkeypatch.setattr(scs, "_monotonic", lambda: now[0])
    await crypto.initialise_for_space("sp-1")
    assert await crypto.mr_key_for_kid("A" * 22) is None  # builds
    calls = []
    real = repo.list_recent

    async def counting(*, per_space):
        calls.append(per_space)
        return await real(per_space=per_space)

    monkeypatch.setattr(repo, "list_recent", counting)
    raw = b"\x09" * 32
    await repo.save(
        scs.SpaceKey(
            space_id="sp-1",
            epoch=5,
            content_key_hex=crypto._kek.encrypt(raw, associated_data=b"sp-1"),
        )
    )
    kid, key = derive_mr_key(raw)
    for _ in range(5):
        assert await crypto.mr_key_for_kid(kid) is None
    assert calls == []
    now[0] += 61
    assert await crypto.mr_key_for_kid(kid) == ("sp-1", 5, key)
    assert calls == [3]


@pytest.mark.parametrize("kid", ["", "nope", "A" * 21, "A" * 23, "A" * 21 + "="])
async def test_a_malformed_kid_costs_no_lookup_and_no_rebuild(
    crypto_env, monkeypatch, kid
):
    crypto, repo = crypto_env
    await crypto.initialise_for_space("sp-1")
    calls = []

    async def counting(*, per_space):
        calls.append(per_space)
        return []

    monkeypatch.setattr(repo, "list_recent", counting)
    # Cold cache (never built) and a stale one alike.
    assert await crypto.mr_key_for_kid(kid) is None
    assert calls == []


async def test_the_kid_cache_drops_keys_removed_behind_its_back_after_ten_minutes(
    crypto_env, monkeypatch
):
    """A key deleted outside the service (a space delete cascade, a restore)
    stops opening frames once the cache is older than ten minutes — even
    when every lookup in between was a hit."""
    crypto, repo = crypto_env
    now = [1000.0]
    monkeypatch.setattr(scs, "_monotonic", lambda: now[0])
    await crypto.initialise_for_space("sp-1")
    _, kid, _ = await _current_mr(crypto, "sp-1")
    assert await crypto.mr_key_for_kid(kid) is not None  # builds
    await repo._db.enqueue("DELETE FROM space_keys WHERE space_id='sp-1'")
    now[0] += scs.MR_KID_CACHE_MAX_AGE_S - 1
    assert await crypto.mr_key_for_kid(kid) is not None  # still cached
    now[0] += 2
    assert await crypto.mr_key_for_kid(kid) is None
