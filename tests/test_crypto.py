"""Tests for socialhome.crypto."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.crypto import (
    ReplayCache,
    b64url_decode,
    b64url_encode,
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
    generate_routing_secret,
    generate_x25519_keypair,
    keyed_hash,
    random_token,
    sha256_hex,
    sign_ed25519,
    sign_user_assertion,
    verify_ed25519,
    verify_user_identity_assertion,
    x25519_exchange,
)
from socialhome.domain.user import UserIdentityAssertion


def test_derive_instance_id_deterministic():
    """derive_instance_id returns the same value for the same key."""
    kp = generate_identity_keypair()
    assert derive_instance_id(kp.public_key) == derive_instance_id(kp.public_key)


def test_derive_instance_id_length():
    """derive_instance_id produces a 32-character string."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    assert len(iid) == 32


def test_derive_instance_id_different_keys_differ():
    """Two different keypairs yield different instance IDs."""
    a = generate_identity_keypair()
    b = generate_identity_keypair()
    assert derive_instance_id(a.public_key) != derive_instance_id(b.public_key)


def test_derive_instance_id_rejects_bad_key_length():
    """derive_instance_id raises ValueError for a key shorter than 32 bytes."""
    with pytest.raises(ValueError):
        derive_instance_id(b"short")


def test_derive_user_id_deterministic():
    """derive_user_id is stable for a given key and username."""
    kp = generate_identity_keypair()
    assert derive_user_id(kp.public_key, "alice") == derive_user_id(
        kp.public_key, "alice"
    )


def test_derive_user_id_different_username():
    """Different usernames yield different user IDs."""
    kp = generate_identity_keypair()
    assert derive_user_id(kp.public_key, "alice") != derive_user_id(
        kp.public_key, "bob"
    )


def test_derive_user_id_different_key():
    """Same username under different instance keys yields different IDs."""
    a = generate_identity_keypair()
    b = generate_identity_keypair()
    assert derive_user_id(a.public_key, "alice") != derive_user_id(
        b.public_key, "alice"
    )


def test_derive_user_id_empty_username_rejected():
    """Empty username raises ValueError."""
    kp = generate_identity_keypair()
    with pytest.raises(ValueError):
        derive_user_id(kp.public_key, "")


def test_ed25519_sign_verify():
    """A signature produced with a private key verifies with the matching public key."""
    kp = generate_identity_keypair()
    msg = b"hello"
    sig = sign_ed25519(kp.private_key, msg)
    assert verify_ed25519(kp.public_key, msg, sig)


def test_ed25519_wrong_message():
    """Verification fails when the message does not match the signature."""
    kp = generate_identity_keypair()
    sig = sign_ed25519(kp.private_key, b"hello")
    assert not verify_ed25519(kp.public_key, b"wrong", sig)


def test_ed25519_wrong_key():
    """Verification fails when a different public key is used."""
    a = generate_identity_keypair()
    b = generate_identity_keypair()
    sig = sign_ed25519(a.private_key, b"msg")
    assert not verify_ed25519(b.public_key, b"msg", sig)


def test_x25519_shared_secret_agreement():
    """Both sides of an X25519 exchange produce the same shared secret."""
    a = generate_x25519_keypair()
    b = generate_x25519_keypair()
    s1 = x25519_exchange(a.private_key, b.public_key)
    s2 = x25519_exchange(b.private_key, a.public_key)
    assert s1 == s2


def test_x25519_different_peers_different_secrets():
    """A and B share a secret distinct from A and C."""
    a = generate_x25519_keypair()
    b = generate_x25519_keypair()
    c = generate_x25519_keypair()
    assert x25519_exchange(a.private_key, b.public_key) != x25519_exchange(
        a.private_key, c.public_key
    )


def test_user_identity_assertion_sign_and_verify():
    """A fresh assertion signs and verifies cleanly."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    uid = derive_user_id(kp.public_key, "pascal")
    issued = datetime.now(timezone.utc).isoformat()
    sig = sign_user_assertion(
        kp.private_key,
        user_id=uid,
        instance_id=iid,
        username="pascal",
        display_name="Pascal",
        issued_at=issued,
    )
    a = UserIdentityAssertion(
        user_id=uid,
        instance_id=iid,
        username="pascal",
        display_name="Pascal",
        issued_at=issued,
        signature=sig,
    )
    verify_user_identity_assertion(a, kp.public_key)


def test_user_identity_assertion_tampered_display_name():
    """A tampered display_name invalidates the signature."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    uid = derive_user_id(kp.public_key, "pascal")
    issued = datetime.now(timezone.utc).isoformat()
    sig = sign_user_assertion(
        kp.private_key,
        user_id=uid,
        instance_id=iid,
        username="pascal",
        display_name="Pascal",
        issued_at=issued,
    )
    bad = UserIdentityAssertion(
        user_id=uid,
        instance_id=iid,
        username="pascal",
        display_name="TAMPERED",
        issued_at=issued,
        signature=sig,
    )
    with pytest.raises(ValueError, match="Invalid"):
        verify_user_identity_assertion(bad, kp.public_key)


def test_user_identity_assertion_wrong_instance_id():
    """A mismatched instance_id is caught before verifying the signature."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    uid = derive_user_id(kp.public_key, "pascal")
    issued = datetime.now(timezone.utc).isoformat()
    sig = sign_user_assertion(
        kp.private_key,
        user_id=uid,
        instance_id=iid,
        username="pascal",
        display_name="P",
        issued_at=issued,
    )
    bad = UserIdentityAssertion(
        user_id=uid,
        instance_id="wrong_id",
        username="pascal",
        display_name="P",
        issued_at=issued,
        signature=sig,
    )
    with pytest.raises(ValueError, match="instance_id"):
        verify_user_identity_assertion(bad, kp.public_key)


def test_b64url_roundtrip():
    """b64url_encode / b64url_decode round-trips arbitrary bytes."""
    data = b"hello world \x00\xff"
    assert b64url_decode(b64url_encode(data)) == data


def test_replay_cache_seen_twice():
    """The second call to seen() for the same ID returns True."""
    rc = ReplayCache()
    assert rc.seen("x") is False
    assert rc.seen("x") is True


def test_replay_cache_different_ids_independent():
    """Seeing ID 'a' does not mark ID 'b' as seen."""
    rc = ReplayCache()
    rc.seen("a")
    assert rc.seen("b") is False


def test_replay_cache_load_from_persistence():
    """Replay IDs loaded from storage are immediately considered seen."""
    rc = ReplayCache()
    rc.load([("m1", datetime.now(timezone.utc).isoformat())])
    assert rc.seen("m1") is True
    assert rc.seen("m2") is False


def test_crypto_helpers_random_token():
    """random_token returns a URL-safe string of sufficient length."""
    t = random_token(32)
    assert len(t) > 40


def test_crypto_helpers_sha256_hex_bytes():
    """sha256_hex on bytes returns lowercase hex of length 64."""
    assert len(sha256_hex(b"hello")) == 64


def test_crypto_helpers_sha256_hex_str():
    """sha256_hex on str auto-encodes to utf-8, matching the bytes result."""
    assert sha256_hex("hello") == sha256_hex(b"hello")


def test_crypto_helpers_generate_routing_secret():
    """Routing secret is 64 hex chars (32 bytes)."""
    s = generate_routing_secret()
    assert len(s) == 64


def test_crypto_helpers_keyed_hash():
    """keyed_hash returns a 32-byte HMAC-SHA256 digest."""
    h = keyed_hash("aa" * 32, b"data")
    assert len(h) == 32


# ─── Defensive edge paths ─────────────────────────────────────────────────


def test_verify_ed25519_bad_key_length():
    """verify_ed25519 returns False for wrong-length public key."""
    assert verify_ed25519(b"short", b"msg", b"sig") is False


def test_verify_ed25519_invalid_signature():
    """verify_ed25519 returns False for corrupted signature."""
    kp = generate_identity_keypair()
    sig = sign_ed25519(kp.private_key, b"msg")
    assert verify_ed25519(kp.public_key, b"msg", sig[:32]) is False


def test_sign_ed25519_bad_seed():
    """sign_ed25519 with wrong-length seed raises ValueError."""
    with pytest.raises(ValueError):
        sign_ed25519(b"short", b"msg")


def test_x25519_bad_key_length():
    """x25519_exchange with wrong key length raises ValueError."""
    with pytest.raises(ValueError):
        x25519_exchange(b"short", b"also-short")


def test_derive_instance_id_bad_key():
    """derive_instance_id with wrong length raises ValueError."""
    with pytest.raises(ValueError):
        derive_instance_id(b"not-32-bytes")


def test_replay_cache_prune_removes_old_entries():
    """ReplayCache.prune removes entries older than the window."""
    rc = ReplayCache(window=timedelta(seconds=1))
    past = datetime.now(timezone.utc) - timedelta(seconds=10)
    rc._seen["old"] = past
    rc.prune(now=datetime.now(timezone.utc))
    assert "old" not in rc._seen
