"""Tests for the per-instance X25519 key-wrap keypair (Phase 5b foundation).

``ensure_instance_identity`` mints + persists an Ed25519 identity keypair and
(this change) an X25519 *key-wrap* keypair alongside it. The key-wrap private
half is KEK-wrapped at rest, mirroring the Ed25519 seed / space identity seed.
"""

from __future__ import annotations

import pytest

from socialhome.crypto import b64url_decode, derive_user_id, verify_ed25519
from socialhome.federation.keywrap_seal import open_keywrap, seal_to_keywrap
from socialhome.identity_bootstrap import (
    derive_local_user_id,
    ensure_instance_identity,
)
from socialhome.infrastructure.key_manager import KeyManager


@pytest.fixture
def key_manager():
    return KeyManager(b"\x11" * 32)


async def test_fresh_identity_has_keywrap_keypair(db, key_manager):
    mat = await ensure_instance_identity(db, key_manager, display_name="Home")
    assert mat.keywrap_public_key is not None
    assert len(mat.keywrap_public_key) == 32
    assert mat.keywrap_private_key is not None
    assert len(mat.keywrap_private_key) == 32


async def test_keywrap_private_is_stored_kek_wrapped(db, key_manager):
    mat = await ensure_instance_identity(db, key_manager)
    row = await db.fetchone(
        "SELECT keywrap_private_key, keywrap_public_key FROM instance_identity "
        "WHERE id='self'",
    )
    # Public stored as hex; private stored KEK-wrapped (not plaintext).
    assert row["keywrap_public_key"] == mat.keywrap_public_key.hex()
    stored = row["keywrap_private_key"]
    assert stored
    assert mat.keywrap_private_key.hex() not in stored
    # The wrapped form round-trips through the KEK.
    assert key_manager.decrypt(stored) == mat.keywrap_private_key


async def test_keywrap_keypair_round_trips_a_sealed_box(db, key_manager):
    mat = await ensure_instance_identity(db, key_manager)
    sealed = seal_to_keywrap(
        recipient_keywrap_pub=mat.keywrap_public_key,
        plaintext=b"content key",
    )
    assert (
        open_keywrap(sealed=sealed, recipient_keywrap_priv=mat.keywrap_private_key)
        == b"content key"
    )


async def test_keywrap_keypair_is_stable_across_calls(db, key_manager):
    first = await ensure_instance_identity(db, key_manager)
    second = await ensure_instance_identity(db, key_manager)
    assert second.keywrap_public_key == first.keywrap_public_key
    assert second.keywrap_private_key == first.keywrap_private_key


async def test_lazy_mint_for_preexisting_identity_without_keywrap(db, key_manager):
    """An identity row predating the key-wrap column gets one minted lazily."""
    # First boot mints everything, then simulate the upgrade case by clearing
    # the key-wrap columns as a pre-5b row would have them.
    await ensure_instance_identity(db, key_manager)
    await db.enqueue(
        "UPDATE instance_identity SET keywrap_private_key=NULL, "
        "keywrap_public_key=NULL WHERE id='self'",
    )

    mat = await ensure_instance_identity(db, key_manager)
    assert mat.keywrap_public_key is not None
    assert len(mat.keywrap_public_key) == 32
    row = await db.fetchone(
        "SELECT keywrap_private_key, keywrap_public_key FROM instance_identity "
        "WHERE id='self'",
    )
    assert row["keywrap_public_key"] == mat.keywrap_public_key.hex()
    assert key_manager.decrypt(row["keywrap_private_key"]) == mat.keywrap_private_key


async def test_b64_helper_import_sanity():
    # Guard against an accidental unused-import lint regression in the test.
    assert b64url_decode("AAAA") == b"\x00\x00\x00"


# ── keywrap_sig: the identity self-signs its keywrap pubkey ───────────────
#
# So a sealer (Phase 5b part B) can verify the GFS-served keywrap key is
# genuinely bound to the subscriber identity, never trusting the GFS value.


async def test_fresh_identity_has_keywrap_sig_signed_by_identity(db, key_manager):
    mat = await ensure_instance_identity(db, key_manager)
    assert mat.keywrap_sig
    assert verify_ed25519(
        mat.identity_public_key,
        mat.keywrap_public_key,
        b64url_decode(mat.keywrap_sig),
    )


async def test_keywrap_sig_is_persisted(db, key_manager):
    mat = await ensure_instance_identity(db, key_manager)
    row = await db.fetchone(
        "SELECT keywrap_sig FROM instance_identity WHERE id='self'",
    )
    assert row["keywrap_sig"] == mat.keywrap_sig


async def test_keywrap_sig_is_stable_across_calls(db, key_manager):
    first = await ensure_instance_identity(db, key_manager)
    second = await ensure_instance_identity(db, key_manager)
    assert second.keywrap_sig == first.keywrap_sig


async def test_lazy_mint_keywrap_sig_for_preexisting_identity(db, key_manager):
    """A pre-upgrade row that had a keywrap key but no sig gets the sig
    minted (signed by the existing identity) on next boot."""
    await ensure_instance_identity(db, key_manager)
    # Simulate a pre-safeguard row: keywrap key present, but no sig column value.
    await db.enqueue(
        "UPDATE instance_identity SET keywrap_sig=NULL WHERE id='self'",
    )

    mat = await ensure_instance_identity(db, key_manager)
    assert mat.keywrap_sig
    assert verify_ed25519(
        mat.identity_public_key,
        mat.keywrap_public_key,
        b64url_decode(mat.keywrap_sig),
    )
    row = await db.fetchone(
        "SELECT keywrap_sig FROM instance_identity WHERE id='self'",
    )
    assert row["keywrap_sig"] == mat.keywrap_sig


async def test_lazy_minted_keywrap_key_also_gets_a_sig(db, key_manager):
    """A pre-5b row with NO keywrap key at all → both key and sig minted."""
    await ensure_instance_identity(db, key_manager)
    await db.enqueue(
        "UPDATE instance_identity SET keywrap_private_key=NULL, "
        "keywrap_public_key=NULL, keywrap_sig=NULL WHERE id='self'",
    )

    mat = await ensure_instance_identity(db, key_manager)
    assert mat.keywrap_sig
    assert verify_ed25519(
        mat.identity_public_key,
        mat.keywrap_public_key,
        b64url_decode(mat.keywrap_sig),
    )


# ── derive_local_user_id ─────────────────────────────────────────────────────


async def test_derive_local_user_id_matches_the_crypto_primitive(db, key_manager):
    """The one minting rule for username-anchored local users.

    A peer re-derives exactly this to self-certify an author, so any drift
    between this helper and :func:`derive_user_id` silently drops the user's
    federated content.
    """
    mat = await ensure_instance_identity(db, key_manager)
    got = await derive_local_user_id(db, "alice")
    assert got == derive_user_id(mat.identity_public_key, "alice")
    assert not got.startswith("uid-")


async def test_derive_local_user_id_is_stable_across_calls(db, key_manager):
    await ensure_instance_identity(db, key_manager)
    assert await derive_local_user_id(db, "alice") == await derive_local_user_id(
        db, "alice"
    )


async def test_derive_local_user_id_without_identity_raises(db):
    """Deriving from a missing key must fail loudly, never fall back."""
    with pytest.raises(RuntimeError, match="instance_identity not initialised"):
        await derive_local_user_id(db, "alice")
