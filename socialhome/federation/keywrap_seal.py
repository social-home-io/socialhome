"""Static-recipient sealed-box (ECIES) for key-wrapping to a published pubkey.

Foundation for Phase 5b — sealing a payload (later: the per-space content
key) to a household that is **not** a paired peer and without any online
ephemeral discovery. The recipient publishes a long-lived X25519 *key-wrap*
public key (at GFS registration, see ``gfs_connection_service`` /
``ClientInstance.keywrap_public_key``); the sender seals to it directly.

This is the classic sealed-box / ECIES shape, distinct from
:mod:`socialhome.federation.routed_crypto` (which negotiates a *target
ephemeral* key via online ``SPACE_FIND_ROUTE`` discovery and derives two
directional keys). Here the recipient key is static and known ahead of time,
so there is a single direction and no ephemeral exchange to coordinate:

1. **Sender:** generate a fresh ephemeral X25519 keypair, compute
   ``DH(eph_priv, recipient_keywrap_pub)``, HKDF-SHA256 → one 32-byte
   AES-256-GCM key, encrypt the plaintext, and ship
   ``{kem_suite, eph_pk, ciphertext}`` (a fresh ephemeral per call → no key
   reuse, so two seals of the same plaintext differ).
2. **Recipient:** ``DH(keywrap_priv, eph_pk)`` yields the same shared secret,
   the same HKDF key, and AES-GCM-decrypts.

The ephemeral pub is bound into the AEAD as additional-authenticated-data so
a relay can't swap it for its own pub (which would otherwise let it substitute
a ciphertext it can read); on mismatch the tag fails.

## Wire shape (the dict the handoff event embeds)

```python
{
    "kem_suite":  "x25519",          # algo for the key agreement
    "eph_pk":     "<32 b64url>",     # sender's ephemeral pub
    "ciphertext": "<nonce b64url>:<aead b64url>",  # AES-GCM nonce:ct||tag
}
```

``kem_suite`` mirrors the ``sig_suite`` / routed-crypto suite mechanism
(``docs/crypto.md`` § "The suite contract"): receivers reject suites they
don't know — **never** fall back to a default — so the Phase-2 PQ migration
(``x25519+mlkem768`` hybrid: concatenated X25519 + ML-KEM material in
``eph_pk``, double KEM → HKDF over the combined secret) is a wire-additive
change with no envelope-shape break.

Pure: depends only on ``cryptography`` + :mod:`socialhome.crypto`. No I/O.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives import hashes as _hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..crypto import (
    X25519Keypair,
    b64url_decode,
    b64url_encode,
    derive_instance_id,
    generate_x25519_keypair,
    verify_ed25519,
    x25519_exchange,
)

#: KEM suite this build implements. PQ migration (Phase 2 of
#: ``docs/crypto.md``) layers ML-KEM-768 on top of X25519 → the value would
#: become ``"x25519+mlkem768"`` and ``eph_pk`` would carry the concatenated
#: X25519 + ML-KEM material; the envelope shape is otherwise unchanged.
KEM_SUITE_X25519: str = "x25519"
SUPPORTED_KEM_SUITES: frozenset[str] = frozenset({KEM_SUITE_X25519})

#: HKDF domain-separation tag — distinct from routed_crypto's info strings so
#: a key derived for the routed-mesh direction can never collide with a
#: key-wrap key even on an (impossible) shared shared-secret.
_HKDF_INFO: bytes = b"space-keywrap:v1"


class UnsupportedKemSuite(ValueError):
    """Raised when a sealed payload advertises a KEM suite this build doesn't
    know (or omits it). Receivers MUST reject rather than fall back to a
    weaker suite — otherwise a downgrade attack becomes possible once the
    Phase-2 hybrid suite ships alongside this one."""


def _require_known_suite(sealed: dict[str, str]) -> None:
    # No default-on-missing: the first-revision wire always ships ``kem_suite``
    # (single supported value today), so a missing field is malformed, not a
    # legacy default. Reject it.
    suite = sealed.get("kem_suite")
    if suite not in SUPPORTED_KEM_SUITES:
        raise UnsupportedKemSuite(
            f"sealed payload advertises unsupported kem_suite={suite!r}; "
            f"this build supports {sorted(SUPPORTED_KEM_SUITES)!r}",
        )


def _derive_key(shared: bytes) -> bytes:
    return HKDF(
        algorithm=_hashes.SHA256(),
        length=32,
        salt=None,
        info=_HKDF_INFO,
    ).derive(shared)


def _aad(eph_pk_b64: str) -> bytes:
    """Bind the ciphertext to the sender ephemeral pub.

    A relay that swaps ``eph_pk`` for its own (to substitute a ciphertext it
    can decrypt) is caught by the AEAD tag — the recipient derives its key
    from the *advertised* ``eph_pk`` but verifies the tag over the same value,
    so any mismatch between the pub used for DH and the pub in the envelope
    fails the open.
    """
    return b"socialhome/space_keywrap/v1|" + eph_pk_b64.encode("ascii")


def seal_to_keywrap(
    *,
    recipient_keywrap_pub: bytes,
    plaintext: bytes,
) -> dict[str, str]:
    """Seal *plaintext* to a recipient's static X25519 key-wrap public key.

    Generates a fresh ephemeral keypair per call (no key reuse), derives an
    AES-256-GCM key via ``DH(eph_priv, recipient_pub)`` + HKDF, and returns
    the ``{kem_suite, eph_pk, ciphertext}`` wire dict.

    Fail-closed: a non-32-byte recipient key raises :class:`ValueError`
    (via :func:`socialhome.crypto.x25519_exchange`).
    """
    eph: X25519Keypair = generate_x25519_keypair()
    shared = x25519_exchange(eph.private_key, recipient_keywrap_pub)
    key = _derive_key(shared)

    eph_pk_b64 = b64url_encode(eph.public_key)
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext, _aad(eph_pk_b64))
    return {
        "kem_suite": KEM_SUITE_X25519,
        "eph_pk": eph_pk_b64,
        "ciphertext": f"{b64url_encode(nonce)}:{b64url_encode(ct)}",
    }


def open_keywrap(
    *,
    sealed: dict[str, str],
    recipient_keywrap_priv: bytes,
) -> bytes:
    """Open a payload sealed via :func:`seal_to_keywrap`.

    Rejects an unknown / missing ``kem_suite`` with
    :class:`UnsupportedKemSuite` (no default fallback). Raises
    :class:`ValueError` on a malformed wire shape or a non-32-byte private
    key, and :class:`cryptography.exceptions.InvalidTag` on a failed AEAD tag
    (wrong recipient key, tampered ciphertext, or swapped ``eph_pk``). The
    caller drops the payload on any raise.
    """
    _require_known_suite(sealed)
    try:
        eph_pk_b64 = sealed["eph_pk"]
        nonce_b64, ct_b64 = sealed["ciphertext"].split(":", 1)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"malformed sealed key-wrap payload: {exc}") from exc

    shared = x25519_exchange(recipient_keywrap_priv, b64url_decode(eph_pk_b64))
    key = _derive_key(shared)
    return AESGCM(key).decrypt(
        b64url_decode(nonce_b64),
        b64url_decode(ct_b64),
        _aad(eph_pk_b64),
    )


def keywrap_wire_fields(public_key: bytes, keywrap_sig: str) -> dict[str, str]:
    """The ``keywrap_pk`` / ``keywrap_sig`` / ``keywrap_suite`` fields a
    household ships so a peer can seal to its static key-wrap key.

    One shape for every carrier — a GFS-reach pairing code, a signed
    peer-accept and ``INSTANCE_CAPABILITIES_UPDATED`` (v_54) — so the
    receiver's check (suite known, key bound to the identity key) is the
    same everywhere. Always suite-tagged.
    """
    return {
        "keywrap_pk": public_key.hex(),
        "keywrap_sig": keywrap_sig,
        "keywrap_suite": KEM_SUITE_X25519,
    }


def verify_keywrap_binding(
    *,
    instance_id: str,
    identity_pub: bytes,
    keywrap_pub: bytes,
    keywrap_sig: str,
) -> bool:
    """Verify a subscriber's key-wrap pubkey is bound to its identity, E2E.

    A sealer (Phase 5b content-key handoff) learns a subscriber's key-wrap
    pubkey *from the GFS*. A malicious GFS could substitute a key-wrap pubkey
    **it** controls and read the sealed content key. This safeguard binds the
    key-wrap key to the subscriber identity so the sealer never trusts the
    GFS-served value — it MUST call this on the GFS-served
    ``{instance_id, identity public_key, keywrap_public_key, keywrap_sig}``
    *before* sealing. Mirrors the #596 ``derive_instance_id`` trust pattern.

    Returns ``True`` only when ALL hold (else ``False`` — fail-closed, never
    raises on bad input):

    1. ``derive_instance_id(identity_pub) == instance_id`` — binds the
       identity key to the claimed id (160-bit; the GFS can't forge an
       identity key whose id matches a substituted instance_id);
    2. ``len(keywrap_pub) == 32`` — a well-formed X25519 public key;
    3. ``verify_ed25519(identity_pub, keywrap_pub, b64url_decode(keywrap_sig))``
       — the key-wrap key is genuinely self-signed by that identity.

    A substituted key-wrap key (no valid signature from the real identity)
    fails (3); a forged identity fails (1) — so the GFS-swap attack is
    rejected at the sealer.
    """
    try:
        if derive_instance_id(identity_pub) != instance_id:
            return False
        if len(keywrap_pub) != 32:
            return False
        if not keywrap_sig:
            return False
        raw_sig = b64url_decode(keywrap_sig)
        return verify_ed25519(identity_pub, keywrap_pub, raw_sig)
    except ValueError, TypeError:
        return False


__all__ = [
    "KEM_SUITE_X25519",
    "SUPPORTED_KEM_SUITES",
    "UnsupportedKemSuite",
    "keywrap_wire_fields",
    "open_keywrap",
    "seal_to_keywrap",
    "verify_keywrap_binding",
]
