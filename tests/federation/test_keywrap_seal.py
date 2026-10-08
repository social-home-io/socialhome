"""Tests for the static-recipient sealed-box key-wrap primitive.

Foundation for Phase 5b subscriber content-key handoff: seal a payload
to a household's published X25519 key-wrap pubkey (GFS-blind, no online
ephemeral discovery). Mirrors the suite-tagged, PQ-forward shape of
``socialhome.federation.routed_crypto``.
"""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.federation.keywrap_seal import (
    KEM_SUITE_X25519,
    SUPPORTED_KEM_SUITES,
    UnsupportedKemSuite,
    keywrap_wire_fields,
    open_keywrap,
    seal_to_keywrap,
    verify_keywrap_binding,
)


def test_keywrap_wire_fields_are_suite_tagged():
    """One shape for every carrier (pairing code, peer-accept,
    capabilities) — always with the suite tag."""
    assert keywrap_wire_fields(b"\x01" * 32, "sig") == {
        "keywrap_pk": "01" * 32,
        "keywrap_sig": "sig",
        "keywrap_suite": KEM_SUITE_X25519,
    }


def test_kem_suite_constant_is_in_supported_set():
    assert KEM_SUITE_X25519 == "x25519"
    assert KEM_SUITE_X25519 in SUPPORTED_KEM_SUITES


def test_round_trip():
    kp = generate_x25519_keypair()
    pt = b"the per-space content key would live here"
    sealed = seal_to_keywrap(recipient_keywrap_pub=kp.public_key, plaintext=pt)
    assert open_keywrap(sealed=sealed, recipient_keywrap_priv=kp.private_key) == pt


def test_wire_shape_fields():
    kp = generate_x25519_keypair()
    sealed = seal_to_keywrap(recipient_keywrap_pub=kp.public_key, plaintext=b"x")
    assert sealed["kem_suite"] == KEM_SUITE_X25519
    assert "eph_pk" in sealed
    assert "ciphertext" in sealed
    # ciphertext carries ``nonce:ct`` (two b64url segments).
    assert sealed["ciphertext"].count(":") == 1


def test_empty_plaintext_round_trips():
    kp = generate_x25519_keypair()
    sealed = seal_to_keywrap(recipient_keywrap_pub=kp.public_key, plaintext=b"")
    assert open_keywrap(sealed=sealed, recipient_keywrap_priv=kp.private_key) == b""


def test_wrong_recipient_priv_fails_aead():
    kp = generate_x25519_keypair()
    other = generate_x25519_keypair()
    sealed = seal_to_keywrap(recipient_keywrap_pub=kp.public_key, plaintext=b"secret")
    with pytest.raises(InvalidTag):
        open_keywrap(sealed=sealed, recipient_keywrap_priv=other.private_key)


def test_unknown_kem_suite_rejected_no_fallback():
    kp = generate_x25519_keypair()
    sealed = seal_to_keywrap(recipient_keywrap_pub=kp.public_key, plaintext=b"x")
    sealed["kem_suite"] = "x25519+mlkem768"  # a future suite this build lacks
    with pytest.raises(UnsupportedKemSuite):
        open_keywrap(sealed=sealed, recipient_keywrap_priv=kp.private_key)


def test_missing_kem_suite_rejected():
    kp = generate_x25519_keypair()
    sealed = seal_to_keywrap(recipient_keywrap_pub=kp.public_key, plaintext=b"x")
    del sealed["kem_suite"]
    with pytest.raises(UnsupportedKemSuite):
        open_keywrap(sealed=sealed, recipient_keywrap_priv=kp.private_key)


def test_tampered_ciphertext_raises():
    kp = generate_x25519_keypair()
    sealed = seal_to_keywrap(recipient_keywrap_pub=kp.public_key, plaintext=b"secret")
    nonce_b64, ct_b64 = sealed["ciphertext"].split(":")
    # Flip a character in the ciphertext segment.
    flipped = ("A" if ct_b64[0] != "A" else "B") + ct_b64[1:]
    sealed["ciphertext"] = f"{nonce_b64}:{flipped}"
    with pytest.raises(InvalidTag):
        open_keywrap(sealed=sealed, recipient_keywrap_priv=kp.private_key)


def test_tampered_eph_pk_raises():
    kp = generate_x25519_keypair()
    sealed = seal_to_keywrap(recipient_keywrap_pub=kp.public_key, plaintext=b"secret")
    other = generate_x25519_keypair()
    from socialhome.crypto import b64url_encode

    sealed["eph_pk"] = b64url_encode(other.public_key)
    with pytest.raises(InvalidTag):
        open_keywrap(sealed=sealed, recipient_keywrap_priv=kp.private_key)


def test_each_seal_uses_a_fresh_ephemeral():
    kp = generate_x25519_keypair()
    pt = b"same plaintext"
    a = seal_to_keywrap(recipient_keywrap_pub=kp.public_key, plaintext=pt)
    b = seal_to_keywrap(recipient_keywrap_pub=kp.public_key, plaintext=pt)
    # No ephemeral reuse → different eph_pk and different ciphertext.
    assert a["eph_pk"] != b["eph_pk"]
    assert a["ciphertext"] != b["ciphertext"]
    # Both still decrypt to the same plaintext.
    assert open_keywrap(sealed=a, recipient_keywrap_priv=kp.private_key) == pt
    assert open_keywrap(sealed=b, recipient_keywrap_priv=kp.private_key) == pt


def test_short_recipient_pub_rejected():
    with pytest.raises(ValueError):
        seal_to_keywrap(recipient_keywrap_pub=b"\x00" * 16, plaintext=b"x")


def test_short_recipient_priv_rejected():
    kp = generate_x25519_keypair()
    sealed = seal_to_keywrap(recipient_keywrap_pub=kp.public_key, plaintext=b"x")
    with pytest.raises(ValueError):
        open_keywrap(sealed=sealed, recipient_keywrap_priv=b"\x00" * 16)


# ── verify_keywrap_binding — the GFS-substitution safeguard ───────────────
#
# A sealer learns a subscriber's keywrap pubkey FROM THE GFS. A malicious GFS
# could swap in a keywrap key it controls and read the sealed content key.
# ``verify_keywrap_binding`` binds the keywrap key to the subscriber identity
# end-to-end (mirrors the #596 derive_instance_id pattern) so the sealer never
# trusts the GFS-served value. Fail-closed: returns False on any bad input.


def _genuine_binding():
    """Mint an identity + keywrap key with a valid self-signature binding."""
    idkp = generate_identity_keypair()
    kw = generate_x25519_keypair()
    sig = b64url_encode(sign_ed25519(idkp.private_key, kw.public_key))
    return {
        "instance_id": derive_instance_id(idkp.public_key),
        "identity_pub": idkp.public_key,
        "keywrap_pub": kw.public_key,
        "keywrap_sig": sig,
    }


def test_verify_keywrap_binding_genuine_is_true():
    b = _genuine_binding()
    assert verify_keywrap_binding(
        instance_id=b["instance_id"],
        identity_pub=b["identity_pub"],
        keywrap_pub=b["keywrap_pub"],
        keywrap_sig=b["keywrap_sig"],
    )


def test_verify_keywrap_binding_substituted_keywrap_is_rejected():
    """The GFS-swap attack: an attacker replaces keywrap_pub with a key it
    controls. The signature (from the real identity) no longer matches → False,
    so the sealer never seals the content key to the GFS-controlled key."""
    b = _genuine_binding()
    attacker = generate_x25519_keypair()
    assert (
        verify_keywrap_binding(
            instance_id=b["instance_id"],
            identity_pub=b["identity_pub"],
            keywrap_pub=attacker.public_key,  # swapped
            keywrap_sig=b["keywrap_sig"],  # signs the REAL key, not this one
        )
        is False
    )


def test_verify_keywrap_binding_instance_id_mismatch_is_rejected():
    """A GFS that serves an identity key not matching the claimed instance_id
    is rejected (the id binds the identity key — 160-bit, can't be forged)."""
    b = _genuine_binding()
    other = generate_identity_keypair()
    assert (
        verify_keywrap_binding(
            instance_id=derive_instance_id(other.public_key),  # != identity_pub's id
            identity_pub=b["identity_pub"],
            keywrap_pub=b["keywrap_pub"],
            keywrap_sig=b["keywrap_sig"],
        )
        is False
    )


@pytest.mark.parametrize("bad_len", [31, 33])
def test_verify_keywrap_binding_wrong_keywrap_length_is_rejected(bad_len):
    b = _genuine_binding()
    assert (
        verify_keywrap_binding(
            instance_id=b["instance_id"],
            identity_pub=b["identity_pub"],
            keywrap_pub=b"\x00" * bad_len,
            keywrap_sig=b["keywrap_sig"],
        )
        is False
    )


def test_verify_keywrap_binding_empty_sig_is_rejected():
    b = _genuine_binding()
    assert (
        verify_keywrap_binding(
            instance_id=b["instance_id"],
            identity_pub=b["identity_pub"],
            keywrap_pub=b["keywrap_pub"],
            keywrap_sig="",
        )
        is False
    )


def test_verify_keywrap_binding_malformed_sig_is_rejected():
    b = _genuine_binding()
    assert (
        verify_keywrap_binding(
            instance_id=b["instance_id"],
            identity_pub=b["identity_pub"],
            keywrap_pub=b["keywrap_pub"],
            keywrap_sig="!!!not base64!!!",
        )
        is False
    )


def test_verify_keywrap_binding_bad_identity_pub_length_is_rejected():
    b = _genuine_binding()
    assert (
        verify_keywrap_binding(
            instance_id=b["instance_id"],
            identity_pub=b"\x00" * 16,  # not a 32-byte Ed25519 pub
            keywrap_pub=b["keywrap_pub"],
            keywrap_sig=b["keywrap_sig"],
        )
        is False
    )
