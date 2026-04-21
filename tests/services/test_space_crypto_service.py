"""Tests for SpaceContentEncryption + space crypto helpers (§4.3)."""

from __future__ import annotations

import pytest

from social_home.crypto import (
    derive_space_id,
    generate_space_keypair,
)
from social_home.db.database import AsyncDatabase
from social_home.infrastructure.key_manager import KeyManager
from social_home.repositories.space_key_repo import (
    SqliteSpaceKeyRepo,
)
from social_home.services.space_crypto_service import (
    SpaceContentEncryption,
    create_space_identity,
    sign_space_config,
    verify_space_config,
)


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
    seed, pk, sid = create_space_identity()
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


# ─── seal_for_gfs / unseal_from_gfs (§24.10) ──────────────────────────────


async def test_seal_for_gfs_roundtrip(crypto_env):
    """Sender + payload encrypt under the per-epoch key and decrypt back."""
    crypto, _ = crypto_env
    await crypto.initialise_for_space("sp-1")
    sealed = await crypto.seal_for_gfs(
        space_id="sp-1",
        sender_instance_id="my-iid",
        payload_json='{"hello":"world"}',
    )
    assert sealed.space_id == "sp-1"
    assert sealed.epoch == 0
    # GFS sees only ciphertext for sender + payload.
    assert "my-iid" not in sealed.encrypted_sender
    assert "hello" not in sealed.encrypted_payload

    unsealed = await crypto.unseal_from_gfs(sealed)
    assert unsealed.sender_instance_id == "my-iid"
    assert unsealed.payload == {"hello": "world"}


async def test_seal_for_gfs_uses_latest_epoch(crypto_env):
    """After rotate_epoch, seal_for_gfs picks the new key."""
    crypto, _ = crypto_env
    await crypto.initialise_for_space("sp-1")
    await crypto.rotate_epoch("sp-1")
    sealed = await crypto.seal_for_gfs(
        space_id="sp-1",
        sender_instance_id="my-iid",
        payload_json='{"x":1}',
    )
    assert sealed.epoch == 1


async def test_seal_for_gfs_unknown_space_raises(crypto_env):
    crypto, _ = crypto_env
    with pytest.raises(RuntimeError, match="no epoch key"):
        await crypto.seal_for_gfs(
            space_id="missing",
            sender_instance_id="me",
            payload_json="{}",
        )


async def test_unseal_unknown_epoch_raises(crypto_env):
    from social_home.federation.sealed_sender import SealedEnvelope

    crypto, _ = crypto_env
    fake = SealedEnvelope(
        space_id="sp-1",
        epoch=99,
        encrypted_sender="a:b",
        encrypted_payload="a:b",
    )
    with pytest.raises(RuntimeError, match="missing epoch"):
        await crypto.unseal_from_gfs(fake)
