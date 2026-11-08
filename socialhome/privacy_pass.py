"""Privacy Pass publicly verifiable tokens on blind RSA.

Standard building blocks, no home-made scheme:

* **RFC 9474** blind RSA, variant ``RSABSSA-SHA384-PSS-Deterministic``
  (SHA-384, MGF1-SHA-384, 48-byte salt, identity message preparation) —
  :func:`blind`, :func:`blind_sign`, :func:`finalize`, :func:`verify`.
* **RFC 9578 §6** issuance protocol 2 (token type ``0x0002``, 2048-bit
  key): :func:`token_key_id` over the RSASSA-PSS SubjectPublicKeyInfo,
  :class:`TokenRequest` / the 256-byte ``TokenResponse``.
* **RFC 9577** ``TokenChallenge`` and ``Token`` encodings.

The GFS uses it for anonymous mailbox registration tickets
(:mod:`socialhome.global_server.mailbox_tickets`): a household fetches a
batch of blind-signed tokens over its authenticated channel and spends one
per ``POST /gfs/mailbox/register``. The issuer sees only the blinded
messages, so a redeemed token can't be tied to the household that fetched
it.

No maintained Python implementation of RFC 9474 exists on PyPI, so this
module implements it on the ``cryptography`` package: verification (and the
unblinded signature check inside :func:`finalize`) is OpenSSL's
RSASSA-PSS-VERIFY; only EMSA-PSS-ENCODE, the blinding arithmetic and the raw
RSASP1 are written here, and :func:`blind_sign` blinds the base again with
a fresh random factor, so its timing does not depend on the message.
Tests run the RFC 9474 A.3 and RFC 9578 A.2 vectors.

Every wire shape carries a suite tag (:data:`TICKET_SUITE_PP_BLINDRSA`),
validated with no fallback.
"""

from __future__ import annotations

import hashlib
import math
import secrets
import struct
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_der_public_key,
)

#: Suite tag of a ticket wire shape: Privacy Pass token type 0x0002 —
#: RSABSSA-SHA384-PSS-Deterministic, 2048-bit key (RFC 9578 §6).
TICKET_SUITE_PP_BLINDRSA: str = "privacypass-blindrsa2048-sha384-pss"
SUPPORTED_TICKET_SUITES: frozenset[str] = frozenset({TICKET_SUITE_PP_BLINDRSA})

#: RFC 9578 token type 0x0002 and its sizes.
TOKEN_TYPE_BLIND_RSA: int = 0x0002
#: Modulus length in bytes (Nk).
NK: int = 256
#: Bits of the issuer modulus.
MODULUS_BITS: int = 2048
#: PSS salt length (sLen) — the SHA-384 output size.
SALT_LEN: int = 48
#: Size of an encoded Token: type ‖ nonce ‖ challenge_digest ‖ key id ‖ authenticator.
TOKEN_LEN: int = 2 + 32 + 32 + 32 + NK
#: Size of an encoded TokenRequest: type ‖ truncated key id ‖ blinded_msg.
TOKEN_REQUEST_LEN: int = 2 + 1 + NK

#: DER AlgorithmIdentifier for id-RSASSA-PSS with SHA-384 / MGF1-SHA-384 /
#: saltLength 48 — the SPKI form RFC 9578 §6.5 hashes into token_key_id.
_PSS_ALG_ID: bytes = bytes.fromhex(
    "303d06092a864886f70d01010a3030a00d300b0609608648016503040202a11a"
    "301806092a864886f70d010108300b0609608648016503040202a203020130"
)

_HASH = hashlib.sha384
_HLEN = 48


class UnsupportedTicketSuite(ValueError):
    """A ticket wire shape names a suite this side does not speak."""


class InvalidToken(ValueError):
    """Malformed input, a failed blinding / signing step or a token whose
    signature does not verify."""


def validate_ticket_suite(suite: object) -> None:
    """Raise :class:`UnsupportedTicketSuite` unless *suite* is supported
    (no default on a missing value)."""
    if not isinstance(suite, str) or suite not in SUPPORTED_TICKET_SUITES:
        raise UnsupportedTicketSuite(f"unsupported ticket suite: {suite!r}")


# ── Keys ─────────────────────────────────────────────────────────────────


def generate_issuer_key() -> rsa.RSAPrivateKey:
    """A fresh 2048-bit issuer key (e = 65537)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=MODULUS_BITS)


def _der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def encode_public_key(public_key: rsa.RSAPublicKey) -> bytes:
    """The RSASSA-PSS SubjectPublicKeyInfo of *public_key* (RFC 9578 §6.5)."""
    pkcs1 = public_key.public_bytes(Encoding.DER, PublicFormat.PKCS1)
    bit_string = b"\x03" + _der_len(len(pkcs1) + 1) + b"\x00" + pkcs1
    body = _PSS_ALG_ID + bit_string
    return b"\x30" + _der_len(len(body)) + body


def decode_public_key(spki: bytes) -> rsa.RSAPublicKey:
    """Parse an RSASSA-PSS SPKI made by :func:`encode_public_key`. Anything
    else — another algorithm id, another size, trailing bytes — raises
    :class:`InvalidToken`."""
    try:
        key = load_der_public_key(spki)
    except (ValueError, TypeError) as exc:
        raise InvalidToken("unreadable issuer key") from exc
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size != MODULUS_BITS:
        raise InvalidToken("issuer key is not RSA-2048")
    if encode_public_key(key) != spki:
        raise InvalidToken("issuer key is not an RSASSA-PSS SPKI")
    return key


def token_key_id(public_key: rsa.RSAPublicKey) -> bytes:
    """``SHA256(encoded_key)`` — 32 bytes (RFC 9578 §6.5)."""
    return hashlib.sha256(encode_public_key(public_key)).digest()


# ── RFC 9474 ─────────────────────────────────────────────────────────────


def _mgf1(seed: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += _HASH(seed + struct.pack(">I", counter)).digest()
        counter += 1
    return bytes(out[:length])


def emsa_pss_encode(msg: bytes, em_bits: int, salt: bytes) -> bytes:
    """EMSA-PSS-ENCODE (RFC 8017 §9.1.1) with SHA-384 / MGF1-SHA-384."""
    em_len = (em_bits + 7) // 8
    if em_len < _HLEN + len(salt) + 2:
        raise InvalidToken("encoding error")
    m_hash = _HASH(msg).digest()
    h = _HASH(b"\x00" * 8 + m_hash + salt).digest()
    ps = b"\x00" * (em_len - len(salt) - _HLEN - 2)
    db = ps + b"\x01" + salt
    mask = _mgf1(h, em_len - _HLEN - 1)
    masked = bytearray(a ^ b for a, b in zip(db, mask, strict=True))
    masked[0] &= 0xFF >> (8 * em_len - em_bits)
    return bytes(masked) + h + b"\xbc"


def _n_e(public_key: rsa.RSAPublicKey) -> tuple[int, int]:
    nums = public_key.public_numbers()
    return nums.n, nums.e


def blind(
    public_key: rsa.RSAPublicKey,
    msg: bytes,
    *,
    salt: bytes | None = None,
    r: int | None = None,
) -> tuple[bytes, int]:
    """RFC 9474 §4.2 ``Blind``: ``(blinded_msg, inv)``. *salt* and *r* are
    for test vectors only — production draws them at random."""
    n, e = _n_e(public_key)
    k_len = (n.bit_length() + 7) // 8
    salt = secrets.token_bytes(SALT_LEN) if salt is None else salt
    encoded = emsa_pss_encode(msg, n.bit_length() - 1, salt)
    m = int.from_bytes(encoded, "big")
    if math.gcd(m, n) != 1:
        raise InvalidToken("invalid input")
    if r is None:
        r = secrets.randbelow(n - 1) + 1
    try:
        inv = pow(r, -1, n)
    except ValueError as exc:
        raise InvalidToken("blinding error") from exc
    z = (m * pow(r, e, n)) % n
    return z.to_bytes(k_len, "big"), inv


def blind_sign(private_key: rsa.RSAPrivateKey, blinded_msg: bytes) -> bytes:
    """RFC 9474 §4.3 ``BlindSign``. RSASP1 runs on a base blinded with a
    fresh random factor (so its timing does not follow the message), and
    the result is checked with RSAVP1 before it leaves (a fault never
    leaks the key)."""
    priv = private_key.private_numbers()
    n, e = priv.public_numbers.n, priv.public_numbers.e
    k_len = (n.bit_length() + 7) // 8
    if len(blinded_msg) != k_len:
        raise InvalidToken("unexpected input size")
    m = int.from_bytes(blinded_msg, "big")
    if m >= n:
        raise InvalidToken("message representative out of range")
    while True:
        b = secrets.randbelow(n - 1) + 1
        if math.gcd(b, n) == 1:
            break
    base = (m * pow(b, e, n)) % n
    # RSASP1 by CRT (RFC 8017 §5.2.1 2.b) — ~3-4x faster than pow(·, d, n).
    p, q = priv.p, priv.q
    s1 = pow(base % p, priv.dmp1, p)
    s2 = pow(base % q, priv.dmq1, q)
    s = (s2 + q * ((priv.iqmp * (s1 - s2)) % p)) % n
    s = (s * pow(b, -1, n)) % n
    if pow(s, e, n) != m:
        raise InvalidToken("signing failure")
    return s.to_bytes(k_len, "big")


def verify(public_key: rsa.RSAPublicKey, msg: bytes, sig: bytes) -> bool:
    """RSASSA-PSS-VERIFY, SHA-384, MGF1-SHA-384, sLen 48 (OpenSSL)."""
    try:
        public_key.verify(
            sig,
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA384()), salt_length=SALT_LEN),
            hashes.SHA384(),
        )
    except InvalidSignature:
        return False
    return True


def finalize(
    public_key: rsa.RSAPublicKey, msg: bytes, blind_sig: bytes, inv: int
) -> bytes:
    """RFC 9474 §4.4 ``Finalize``: the unblinded signature, checked."""
    n, _e = _n_e(public_key)
    k_len = (n.bit_length() + 7) // 8
    if len(blind_sig) != k_len:
        raise InvalidToken("unexpected input size")
    s = (int.from_bytes(blind_sig, "big") * inv) % n
    sig = s.to_bytes(k_len, "big")
    if not verify(public_key, msg, sig):
        raise InvalidToken("invalid signature")
    return sig


# ── RFC 9577 / 9578 token type 0x0002 ────────────────────────────────────


def token_challenge(
    issuer_name: bytes, *, redemption_context: bytes = b"", origin_info: bytes = b""
) -> bytes:
    """An encoded RFC 9577 §2.1 ``TokenChallenge`` for token type 0x0002."""
    if not 0 < len(issuer_name) < 1 << 16 or len(origin_info) >= 1 << 16:
        raise InvalidToken("challenge field size")
    if len(redemption_context) not in (0, 32):
        raise InvalidToken("redemption_context must be 0 or 32 bytes")
    return (
        struct.pack(">HH", TOKEN_TYPE_BLIND_RSA, len(issuer_name))
        + issuer_name
        + bytes([len(redemption_context)])
        + redemption_context
        + struct.pack(">H", len(origin_info))
        + origin_info
    )


def token_input(nonce: bytes, challenge_digest: bytes, key_id: bytes) -> bytes:
    """``token_type ‖ nonce ‖ challenge_digest ‖ token_key_id`` — what the
    authenticator signs (RFC 9578 §6.1 / §6.4)."""
    return struct.pack(">H", TOKEN_TYPE_BLIND_RSA) + nonce + challenge_digest + key_id


@dataclass(slots=True, frozen=True)
class Token:
    """An RFC 9577 §2.2 ``Token`` of type 0x0002."""

    nonce: bytes
    challenge_digest: bytes
    token_key_id: bytes
    authenticator: bytes

    def encode(self) -> bytes:
        return (
            token_input(self.nonce, self.challenge_digest, self.token_key_id)
            + self.authenticator
        )

    @classmethod
    def decode(cls, raw: bytes) -> Token:
        if len(raw) != TOKEN_LEN:
            raise InvalidToken("token size")
        (token_type,) = struct.unpack(">H", raw[:2])
        if token_type != TOKEN_TYPE_BLIND_RSA:
            raise InvalidToken("token type")
        return cls(raw[2:34], raw[34:66], raw[66:98], raw[98:])

    def verify(self, public_key: rsa.RSAPublicKey) -> bool:
        """The authenticator verifies over this token's input under
        *public_key* (RFC 9578 §6.4)."""
        return verify(
            public_key,
            token_input(self.nonce, self.challenge_digest, self.token_key_id),
            self.authenticator,
        )


@dataclass(slots=True, frozen=True)
class PendingToken:
    """Client state between the request and the response (never sent)."""

    nonce: bytes
    challenge_digest: bytes
    token_key_id: bytes
    inv: int

    def __repr__(self) -> str:  # the blinding inverse stays out of logs
        return "PendingToken(<redacted>)"


def token_request(
    public_key: rsa.RSAPublicKey,
    challenge: bytes,
    *,
    nonce: bytes | None = None,
    salt: bytes | None = None,
    r: int | None = None,
) -> tuple[bytes, PendingToken]:
    """RFC 9578 §6.1: an encoded ``TokenRequest`` and the state
    :func:`finalize_token` needs. *nonce* / *salt* / *r* are for vectors."""
    nonce = secrets.token_bytes(32) if nonce is None else nonce
    digest = hashlib.sha256(challenge).digest()
    key_id = token_key_id(public_key)
    blinded, inv = blind(public_key, token_input(nonce, digest, key_id), salt=salt, r=r)
    request = struct.pack(">HB", TOKEN_TYPE_BLIND_RSA, key_id[-1]) + blinded
    return request, PendingToken(nonce, digest, key_id, inv)


def parse_token_request(raw: bytes) -> tuple[int, bytes]:
    """``(truncated_token_key_id, blinded_msg)`` of an encoded request."""
    if len(raw) != TOKEN_REQUEST_LEN:
        raise InvalidToken("token request size")
    token_type, truncated = struct.unpack(">HB", raw[:3])
    if token_type != TOKEN_TYPE_BLIND_RSA:
        raise InvalidToken("token type")
    return truncated, raw[3:]


def finalize_token(
    public_key: rsa.RSAPublicKey, pending: PendingToken, token_response: bytes
) -> Token:
    """RFC 9578 §6.3: the finished token (raises :class:`InvalidToken` on a
    response that does not unblind to a valid signature)."""
    msg = token_input(pending.nonce, pending.challenge_digest, pending.token_key_id)
    authenticator = finalize(public_key, msg, token_response, pending.inv)
    return Token(
        pending.nonce, pending.challenge_digest, pending.token_key_id, authenticator
    )
