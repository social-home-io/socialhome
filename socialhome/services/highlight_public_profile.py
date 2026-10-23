"""Who shared a public highlight — sealed to the viewer's browser.

The public highlight page (``/highlight/{instance}/{highlight}/{token}`` on
a GFS) now names the author: display name, bio and a small picture. The
author's household sends them in the highlight stream itself, in one
``author_profile`` frame after ``highlight_meta`` — sealed to a key the
viewer's browser made for this one view, so neither transport shows them
to the GFS:

* the DataChannel is DTLS end to end already;
* the GFS-relay fallback pipes the framed bytes through the GFS, which
  only ever sees the sealed frame.

The viewer generates an ephemeral P-256 ECDH key pair (WebCrypto has
P-256 in every supported browser, X25519 not yet) and sends the raw public
key as ``vk`` with its offer or relay request. The author's household
makes its own ephemeral pair, derives ``HKDF-SHA256(ECDH, salt="",
info=PROFILE_SEAL_INFO)`` and encrypts the profile JSON with AES-256-GCM,
the highlight id as associated data. The frame header carries
``seal_suite``, ``epk`` (the author's ephemeral public key) and ``nonce``.

The residual is the one every public highlight has: the GFS minted the
share token, so it could open the link like any viewer. Sealing keeps the
profile out of every byte it relays and stores (it stores none).
"""

from __future__ import annotations

import os

import orjson
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ..crypto import b64url_decode, b64url_encode

#: ECDH P-256 → HKDF-SHA256 → AES-256-GCM.
PROFILE_SEAL_SUITE_P256: str = "p256-hkdf-sha256-aes256gcm"
SUPPORTED_PROFILE_SEAL_SUITES: frozenset[str] = frozenset({PROFILE_SEAL_SUITE_P256})

#: HKDF ``info`` — domain-separates this key from every other derivation.
PROFILE_SEAL_INFO: bytes = b"sh-highlight-public-profile:v1"

#: A raw uncompressed P-256 point is 65 bytes (``0x04 || X || Y``).
_RAW_POINT_LEN = 65


class UnsupportedProfileSealSuite(ValueError):
    """A sealed profile names a suite we don't support."""


def parse_viewer_key(value: object) -> ec.EllipticCurvePublicKey | None:
    """The viewer's ephemeral P-256 public key from its base64url raw
    uncompressed point, or ``None`` for anything else."""
    if not isinstance(value, str) or not value or len(value) > 100:
        return None
    try:
        raw = b64url_decode(value)
    except ValueError:
        return None
    if len(raw) != _RAW_POINT_LEN or raw[0] != 0x04:
        return None
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError:
        return None


def _key(private: ec.EllipticCurvePrivateKey, peer: ec.EllipticCurvePublicKey) -> bytes:
    shared = private.exchange(ec.ECDH(), peer)
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=b"", info=PROFILE_SEAL_INFO
    ).derive(shared)


def _raw(public: ec.EllipticCurvePublicKey) -> bytes:
    return public.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def seal_profile(
    viewer_key: ec.EllipticCurvePublicKey, *, highlight_id: str, profile: dict
) -> tuple[dict, bytes]:
    """``(header_fields, ciphertext)`` for the ``author_profile`` frame."""
    ephemeral = ec.generate_private_key(ec.SECP256R1())
    nonce = os.urandom(12)
    ciphertext = AESGCM(_key(ephemeral, viewer_key)).encrypt(
        nonce,
        orjson.dumps(profile),
        highlight_id.encode(),
    )
    return (
        {
            "seal_suite": PROFILE_SEAL_SUITE_P256,
            "epk": b64url_encode(_raw(ephemeral.public_key())),
            "nonce": b64url_encode(nonce),
        },
        ciphertext,
    )


def open_profile(
    viewer_private: ec.EllipticCurvePrivateKey,
    header: dict,
    ciphertext: bytes,
    *,
    highlight_id: str,
) -> dict:
    """Reference opener (the viewer bundle mirrors it in WebCrypto).

    Raises :class:`UnsupportedProfileSealSuite` for an unknown suite and
    ``ValueError`` / ``InvalidTag`` for anything malformed or tampered.
    """
    if header.get("seal_suite") not in SUPPORTED_PROFILE_SEAL_SUITES:
        raise UnsupportedProfileSealSuite(str(header.get("seal_suite")))
    epk = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), b64url_decode(str(header.get("epk") or ""))
    )
    plaintext = AESGCM(_key(viewer_private, epk)).decrypt(
        b64url_decode(str(header.get("nonce") or "")),
        ciphertext,
        highlight_id.encode(),
    )
    out = orjson.loads(plaintext)
    if not isinstance(out, dict):
        raise TypeError("sealed profile is not an object")
    return out
