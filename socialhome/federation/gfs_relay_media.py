"""Binary media frames over the GFS envelope relay (v_87) — the household half.

A 512 KiB media chunk never fit the JSON relay (~191 KiB sealed envelopes,
and the chunk is base64'ed twice inside one). This module seals the exact
``fed-media-v1`` body a household POSTs to a peer's inbox
(:func:`~socialhome.federation.media_framing.encode_body`: ``\\x00SHM``, the
frame-suite byte, the signed envelope header, the AES-GCM-sealed chunk) to
the recipient's static key-wrap key and wraps it in the outer relay body
(:mod:`socialhome.domain.relay_media_frame`)::

    outer   = "\\x00SHR" | u8 version 1 | to_instance (32 ASCII) | sealed
    sealed  = u8 kem-suite id | eph_pk (32) | nonce (12) | AES-256-GCM(ct || tag)
    plain   = u32 BE media-body length | media body | zero padding

* **Why seal again.** The signed header carries ``from_instance``,
  ``event_type`` and ``space_id`` in plaintext (it is a §24.11 envelope). The
  GFS must learn none of them — the seal leaves it ``(to_instance, time,
  size bucket)``, exactly what a JSON relay envelope shows it.
* **Suite.** The kem-suite byte names the KEM (:data:`KEM_SUITE_IDS`, today
  ``x25519`` = ``0x01``, the same suite the JSON seal tags as ``kem_suite``);
  an unknown byte is refused with
  :class:`~socialhome.federation.keywrap_seal.UnsupportedKemSuite`, never a
  default. The phase-2 hybrid takes a new byte.
* **Binding.** The AEAD's associated data is the outer prefix (magic,
  version, recipient id), the kem-suite byte and ``eph_pk``: a GFS that re-addresses a frame to
  another household, or swaps the ephemeral key, breaks the tag. The key is
  HKDF'd under its own ``info`` (:data:`_HKDF_INFO`), so it never equals a
  JSON-relay seal key even for the same DH output.
* **Padding.** The plaintext is padded to one of
  :data:`RELAY_MEDIA_SIZE_BUCKETS`, so the GFS sees a bucket, not a file
  size (both sides refuse any other sealed length): every full 512 KiB chunk lands in the 516 KiB rung, a file's last
  chunk in a smaller one.

After the open, the media body goes through the **same** tail as the inbox
and the DataChannel (:meth:`~socialhome.federation.federation_service
.FederationService.handle_inbound_media_relay`): the §24.11 pipeline on the
signed header, then ``_finish_media_frame`` (frame/AEAD suites, chunk
decrypt, ``chunk_sha256`` binding). Riding the relay skips nothing.

Pure: no I/O.
"""

from __future__ import annotations

import os
import struct

from cryptography.hazmat.primitives import hashes as _hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..crypto import generate_x25519_keypair, x25519_exchange
from ..domain.relay_media_frame import (
    RELAY_MEDIA_MAX_BODY_BYTES,
    RELAY_MEDIA_MAX_PLAINTEXT_BYTES,
    RELAY_MEDIA_PREFIX_BYTES,
    RELAY_MEDIA_SEAL_OVERHEAD_BYTES,
    RELAY_MEDIA_SIZE_BUCKETS,
    InvalidRelayMediaBody,
    build_relay_media_body,
    parse_relay_media_body,
)
from .keywrap_seal import KEM_SUITE_X25519, SUPPORTED_KEM_SUITES, UnsupportedKemSuite

#: The kem-suite byte at the start of ``sealed`` → the suite it names. Each
#: value must be in :data:`~socialhome.federation.keywrap_seal.SUPPORTED_KEM_SUITES`.
KEM_SUITE_IDS: dict[int, str] = {0x01: KEM_SUITE_X25519}
_KEM_ID_BY_NAME: dict[str, int] = {v: k for k, v in KEM_SUITE_IDS.items()}

_HKDF_INFO: bytes = b"socialhome/relay-media:v1"
_AAD_TAG: bytes = b"socialhome/relay_media/v1|"
_EPH_PK_BYTES: int = 32
_NONCE_BYTES: int = 12
_TAG_BYTES: int = 16
_LEN = struct.Struct(">I")

#: Bytes ``sealed`` adds around the plaintext: suite byte, eph_pk, nonce, tag.
SEAL_OVERHEAD_BYTES: int = RELAY_MEDIA_SEAL_OVERHEAD_BYTES

#: The largest media body that fits one relay frame.
RELAY_MEDIA_MAX_MEDIA_BODY_BYTES: int = RELAY_MEDIA_MAX_PLAINTEXT_BYTES - _LEN.size


def _derive_key(shared: bytes) -> bytes:
    return HKDF(
        algorithm=_hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO
    ).derive(shared)


def _aad(prefix: bytes, suite_id: int, eph_pk: bytes) -> bytes:
    return _AAD_TAG + bytes(prefix) + bytes([suite_id]) + eph_pk


def _padded(media_body: bytes) -> bytes:
    need = _LEN.size + len(media_body)
    for size in RELAY_MEDIA_SIZE_BUCKETS:
        if need <= size:
            return _LEN.pack(len(media_body)) + media_body + bytes(size - need)
    raise ValueError(
        f"media body of {len(media_body)} bytes does not fit a relay frame "
        f"(max {RELAY_MEDIA_MAX_MEDIA_BODY_BYTES})"
    )


def seal_relay_media(
    *,
    to_instance: str,
    media_body: bytes,
    recipient_keywrap_pub: bytes,
    kem_suite: str = KEM_SUITE_X25519,
) -> bytes:
    """The outer relay body carrying *media_body* sealed to *to_instance*.

    Raises :class:`ValueError` when the body does not fit
    (:data:`RELAY_MEDIA_MAX_MEDIA_BODY_BYTES`) or the recipient key is not a
    32-byte X25519 key; :class:`UnsupportedKemSuite` for an unknown suite.
    """
    suite_id = _KEM_ID_BY_NAME.get(kem_suite)
    if suite_id is None or kem_suite not in SUPPORTED_KEM_SUITES:
        raise UnsupportedKemSuite(f"unsupported kem_suite={kem_suite!r}")
    plaintext = _padded(media_body)
    prefix = build_relay_media_body(to_instance, b"")
    eph = generate_x25519_keypair()
    key = _derive_key(x25519_exchange(eph.private_key, recipient_keywrap_pub))
    nonce = os.urandom(_NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, plaintext, _aad(prefix, suite_id, eph.public_key))
    return prefix + bytes([suite_id]) + eph.public_key + nonce + ct


def open_relay_media(
    body: bytes,
    *,
    own_instance_id: str,
    keywrap_private_key: bytes,
) -> bytes:
    """The media body inside an outer relay body addressed to us.

    Refuses (:class:`ValueError`) a malformed outer body, a frame addressed
    to another household, a seal that does not open under our key-wrap key
    (tampered, re-addressed, or sealed to someone else), and a padded
    plaintext whose length field overruns it; :class:`UnsupportedKemSuite`
    for an unknown kem-suite byte. Never falls back.
    """
    try:
        to_instance, sealed = parse_relay_media_body(body)
    except InvalidRelayMediaBody as exc:
        raise ValueError(f"invalid relay media body: {exc}") from exc
    if to_instance != own_instance_id:
        raise ValueError("relay media frame addressed to another household")
    if len(sealed) < SEAL_OVERHEAD_BYTES + _LEN.size:
        raise ValueError("relay media seal too short")
    suite = KEM_SUITE_IDS.get(sealed[0])
    if suite is None or suite not in SUPPORTED_KEM_SUITES:
        raise UnsupportedKemSuite(f"unknown relay media kem suite id {sealed[0]}")
    eph_pk = bytes(sealed[1 : 1 + _EPH_PK_BYTES])
    nonce_end = 1 + _EPH_PK_BYTES + _NONCE_BYTES
    nonce = bytes(sealed[1 + _EPH_PK_BYTES : nonce_end])
    prefix = bytes(memoryview(body)[:RELAY_MEDIA_PREFIX_BYTES])
    try:
        key = _derive_key(x25519_exchange(keywrap_private_key, eph_pk))
        plaintext = AESGCM(key).decrypt(
            nonce, bytes(sealed[nonce_end:]), _aad(prefix, sealed[0], eph_pk)
        )
    except Exception as exc:
        raise ValueError("relay media frame does not open") from exc
    (length,) = _LEN.unpack_from(plaintext)
    if _LEN.size + length > len(plaintext):
        raise ValueError("relay media length past the plaintext")
    return plaintext[_LEN.size : _LEN.size + length]


__all__ = [
    "KEM_SUITE_IDS",
    "RELAY_MEDIA_MAX_BODY_BYTES",
    "RELAY_MEDIA_MAX_MEDIA_BODY_BYTES",
    "RELAY_MEDIA_MAX_PLAINTEXT_BYTES",
    "RELAY_MEDIA_SIZE_BUCKETS",
    "SEAL_OVERHEAD_BYTES",
    "open_relay_media",
    "seal_relay_media",
]
