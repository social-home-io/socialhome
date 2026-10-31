"""Seat keys and sealed seat messages at a group table (v_76).

A message at a table goes to the table's hub, which numbers it and passes
it on. A message *to chosen seats* is sealed by the sender's household to
each target seat's household **seat key**, so the hub — and every other
household — passes on only ciphertext (group-play design 2.3, D3).

Seat keys
---------
Each household at a table holds one X25519 key pair for that table (one
per household, not per person: the household server is the trust
boundary, as for DMs). The public half travels signed with the household's
Ed25519 identity key::

    {"seat_kem_suite": "x25519", "pub": "<b64url 32 B>",
     "sig_suite": "ed25519", "sig": "<b64url 64 B>"}

The signature covers :func:`seat_key_message` — a domain-separated,
length-prefixed encoding of ``(session_id, instance, seat_kem_suite,
pub)`` — so a key can't be moved to another table or household.

Sealing
-------
``seat_kem_suite: "x25519"``: X25519 between the sender household's seat
private key and the target household's seat public key (static–static:
only those two households can derive it, and the receiver knows which
household sent it), HKDF-SHA256 with ``salt = msg_id`` and ``info`` binding
the table and the direction (``from_seat`` → ``to_seat``), AES-256-GCM with
a random 12-byte nonce and the same context plus ``msg_id`` as AAD. One key
per message and direction; a hub can't seal one itself, move one to
another seat or table, or pass it off as from another seat.

Every wire shape carries its suite; a receiver refuses one it doesn't know
(:class:`UnsupportedSeatKemSuite`, :class:`UnsupportedSeatKeySigSuite`) —
never a default (CLAUDE.md, "crypto wire shapes carry a suite tag").
Phase 2 of ``docs/crypto.md`` adds ``"x25519+mlkem768"`` beside it.

Pure functions, no I/O.
"""

from __future__ import annotations

import binascii
import os
import struct
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..crypto import (
    b64url_decode,
    b64url_encode,
    generate_x25519_keypair,
    sign_ed25519,
    verify_ed25519,
    x25519_exchange,
)

#: The key agreement + AEAD a seat key and a sealed seat message use.
SEAT_KEM_SUITE_X25519: str = "x25519"
SUPPORTED_SEAT_KEM_SUITES: frozenset[str] = frozenset({SEAT_KEM_SUITE_X25519})

#: The household identity signature over a seat key.
SEAT_KEY_SIG_SUITE_ED25519: str = "ed25519"
SUPPORTED_SEAT_KEY_SIG_SUITES: frozenset[str] = frozenset({SEAT_KEY_SIG_SUITE_ED25519})

#: Bytes in a ``msg_id`` (b64url on the wire).
MSG_ID_BYTES: int = 16
#: The largest plaintext a seal carries (the live message limit).
SEAL_MAX_PLAINTEXT: int = 64 * 1024
_NONCE_BYTES = 12
_TAG_BYTES = 16
#: A sealed ``ct`` on the wire: the largest plaintext + tag, b64url.
_CT_MAX_CHARS: int = (SEAL_MAX_PLAINTEXT + _TAG_BYTES) * 4 // 3 + 4

_KEY_DOMAIN = b"socialhome/app_group/seat_key/v1"
_SEAL_DOMAIN = b"socialhome/app_group/seat_seal/v1"


class SeatSealError(ValueError):
    """A seat key or a sealed entry out of shape, or one that doesn't
    open (wrong key, moved, changed) — the message is dropped."""


class UnsupportedSeatKemSuite(ValueError):
    """A seat key or sealed entry names a key-agreement suite this build
    doesn't know (or none). Refused — never a fallback."""


class UnsupportedSeatKeySigSuite(ValueError):
    """A seat key names a signature suite this build doesn't know (or
    none). Refused — never a fallback."""


def _fields(domain: bytes, *parts: str | int) -> bytes:
    """*domain* then each part length-prefixed (4-byte big-endian): no
    two different field lists encode the same."""
    out = bytearray(domain)
    for part in parts:
        raw = str(part).encode("utf-8")
        out += struct.pack(">I", len(raw)) + raw
    return bytes(out)


def _b64(raw: object, size: int | None, what: str, *, max_chars: int = 128) -> bytes:
    if not isinstance(raw, str) or not raw or len(raw) > max_chars:
        raise SeatSealError(f"{what} malformed")
    try:
        value = b64url_decode(raw)
    except (binascii.Error, ValueError) as exc:
        raise SeatSealError(f"{what} malformed") from exc
    if size is not None and len(value) != size:
        raise SeatSealError(f"{what} wrong size")
    return value


def new_seat_keypair() -> tuple[bytes, bytes]:
    """A fresh X25519 seat key pair: ``(private, public)``, 32 bytes each."""
    kp = generate_x25519_keypair()
    return kp.private_key, kp.public_key


def new_msg_id() -> str:
    """A sender-chosen message id: :data:`MSG_ID_BYTES` random bytes."""
    return b64url_encode(os.urandom(MSG_ID_BYTES))


def seat_key_message(
    session_id: str, instance: str, kem_suite: str, pub_b64: str
) -> bytes:
    """The bytes a household signs to vouch for its seat key at a table."""
    return _fields(_KEY_DOMAIN, session_id, instance, kem_suite, pub_b64)


def sign_seat_key(*, seed: bytes, session_id: str, instance: str, pub: bytes) -> dict:
    """The wire form of this household's seat key, signed with its
    identity key *seed*."""
    pub_b64 = b64url_encode(pub)
    sig = sign_ed25519(
        seed, seat_key_message(session_id, instance, SEAT_KEM_SUITE_X25519, pub_b64)
    )
    return {
        "seat_kem_suite": SEAT_KEM_SUITE_X25519,
        "pub": pub_b64,
        "sig_suite": SEAT_KEY_SIG_SUITE_ED25519,
        "sig": b64url_encode(sig),
    }


def parse_seat_key(raw: object) -> dict[str, str]:
    """A seat key off the wire, its shape and suites checked — the same
    four fields, nothing else. Raises :class:`SeatSealError` /
    :class:`UnsupportedSeatKemSuite` / :class:`UnsupportedSeatKeySigSuite`."""
    if not isinstance(raw, dict):
        raise SeatSealError("seat key malformed")
    if raw.get("seat_kem_suite") not in SUPPORTED_SEAT_KEM_SUITES:
        raise UnsupportedSeatKemSuite(f"seat_kem_suite {raw.get('seat_kem_suite')!r}")
    if raw.get("sig_suite") not in SUPPORTED_SEAT_KEY_SIG_SUITES:
        raise UnsupportedSeatKeySigSuite(f"sig_suite {raw.get('sig_suite')!r}")
    _b64(raw.get("pub"), 32, "pub")
    _b64(raw.get("sig"), 64, "sig")
    return {
        "seat_kem_suite": str(raw["seat_kem_suite"]),
        "pub": str(raw["pub"]),
        "sig_suite": str(raw["sig_suite"]),
        "sig": str(raw["sig"]),
    }


def verify_seat_key(
    raw: object, *, session_id: str, instance: str, identity_pk: bytes
) -> bool:
    """Whether *raw* is *instance*'s seat key for *session_id*, signed by
    the identity key *identity_pk* (raw 32 bytes). Unknown suites raise;
    a malformed key or a bad signature is ``False``."""
    try:
        key = parse_seat_key(raw)
    except SeatSealError:
        return False
    message = seat_key_message(session_id, instance, key["seat_kem_suite"], key["pub"])
    return verify_ed25519(identity_pk, message, b64url_decode(key["sig"]))


def _key_and_aad(
    my_priv: bytes,
    peer_pub: bytes,
    *,
    session_id: str,
    from_seat: int,
    to_seat: int,
    msg_id: str,
) -> tuple[bytes, bytes]:
    salt = _b64(msg_id, MSG_ID_BYTES, "msg_id")
    try:
        shared = x25519_exchange(my_priv, peer_pub)
    except ValueError as exc:  # wrong size, or a low-order point (all-zero secret)
        raise SeatSealError("seat key agreement failed") from exc
    context = _fields(_SEAL_DOMAIN, session_id, from_seat, to_seat)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=context).derive(
        shared
    )
    return key, context + b"|" + msg_id.encode("ascii")


def seal(
    plaintext: bytes,
    *,
    my_priv: bytes,
    peer_pub: bytes,
    session_id: str,
    from_seat: int,
    to_seat: int,
    msg_id: str,
) -> dict[str, Any]:
    """Seal *plaintext* from *from_seat* (this household's seat key
    *my_priv*) to *to_seat* (its household's seat key *peer_pub*): one
    sealed entry ``{to_seat, seat_kem_suite, nonce, ct}``."""
    if len(plaintext) > SEAL_MAX_PLAINTEXT:
        raise SeatSealError("too large to seal")
    key, aad = _key_and_aad(
        my_priv,
        peer_pub,
        session_id=session_id,
        from_seat=from_seat,
        to_seat=to_seat,
        msg_id=msg_id,
    )
    nonce = os.urandom(_NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, plaintext, aad)
    return {
        "to_seat": to_seat,
        "seat_kem_suite": SEAT_KEM_SUITE_X25519,
        "nonce": b64url_encode(nonce),
        "ct": b64url_encode(ct),
    }


def check_sealed_entry(raw: object) -> dict[str, Any]:
    """A sealed entry's shape and suite — what a hub checks before passing
    one on (it can't open it). Raises :class:`SeatSealError` /
    :class:`UnsupportedSeatKemSuite`."""
    if not isinstance(raw, dict):
        raise SeatSealError("sealed entry malformed")
    if raw.get("seat_kem_suite") not in SUPPORTED_SEAT_KEM_SUITES:
        raise UnsupportedSeatKemSuite(f"seat_kem_suite {raw.get('seat_kem_suite')!r}")
    to_seat = raw.get("to_seat")
    if not isinstance(to_seat, int) or isinstance(to_seat, bool) or to_seat <= 0:
        raise SeatSealError("to_seat malformed")
    _b64(raw.get("nonce"), _NONCE_BYTES, "nonce")
    ct = _b64(raw.get("ct"), None, "ct", max_chars=_CT_MAX_CHARS)
    if len(ct) < _TAG_BYTES:
        raise SeatSealError("ct too short")
    return {
        "to_seat": to_seat,
        "seat_kem_suite": str(raw["seat_kem_suite"]),
        "nonce": str(raw["nonce"]),
        "ct": str(raw["ct"]),
    }


def open_sealed(
    raw: object,
    *,
    my_priv: bytes,
    peer_pub: bytes,
    session_id: str,
    from_seat: int,
    msg_id: str,
) -> bytes:
    """Open a sealed entry for this household's seat (``raw["to_seat"]``)
    from *from_seat*, whose household's seat key is *peer_pub*. Raises
    :class:`SeatSealError` when it is out of shape or doesn't open, and
    :class:`UnsupportedSeatKemSuite` for a suite this build doesn't know."""
    entry = check_sealed_entry(raw)
    key, aad = _key_and_aad(
        my_priv,
        peer_pub,
        session_id=session_id,
        from_seat=from_seat,
        to_seat=entry["to_seat"],
        msg_id=msg_id,
    )
    try:
        return AESGCM(key).decrypt(
            b64url_decode(entry["nonce"]), b64url_decode(entry["ct"]), aad
        )
    except Exception as exc:  # InvalidTag: wrong key, moved or changed
        raise SeatSealError("sealed entry doesn't open") from exc
