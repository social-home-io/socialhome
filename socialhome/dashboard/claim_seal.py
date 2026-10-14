"""Sealing a wall panel's connect claim to the panel's own key.

*Connect from Home Assistant* hands a panel its single-use pairing claim
(and the dashboard address to spend it at) through an ESPHome action
called via Home Assistant. HA fires ``call_service`` with the action's
data and its recorder stores it — so the data is sealed to the panel:

* the panel generates an X25519 keypair once (kept in its flash, renewed
  on "Forget server" / factory reset) and publishes the public half in
  its diagnostic text sensor ``Social Home key`` (43-character base64url);
* Social Home reads it from HA and seals ``{"claim", "url"}`` to it with
  ECIES (:func:`seal_claim`): an ephemeral X25519 key, HKDF-SHA256 over
  the shared secret (``salt = epk || panel_pub``, ``info =``
  :data:`PANEL_CLAIM_HKDF_INFO`), AES-256-GCM with a random 12-byte
  nonce and ``AAD = suite || panel_pub``;
* the action carries one variable, ``sealed`` — compact JSON
  ``{"ct", "epk", "nonce", "suite"}`` (bytes as unpadded base64url).

HA (and anyone reading its recorder) sees the ciphertext and the panel's
public key, never the claim or the dashboard address. Only the panel's
private key opens it; :func:`open_sealed_claim` is the reference opener
the firmware mirrors (the shared vectors in
``docs/display-api/v1/fixtures/sealed-claim-vectors.json`` pin both).

The suite travels on the wire (:data:`PANEL_CLAIM_SUITE_X25519_AESGCM`);
receivers reject anything outside :data:`SUPPORTED_PANEL_CLAIM_SUITES`
(:class:`UnsupportedPanelClaimSuite`) — there is no default and no
plaintext form. Phase 2 of ``docs/crypto.md`` (X25519 + ML-KEM-768) is a
sibling suite: a longer ``epk`` / panel key, same shape.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..crypto import (
    b64url_decode,
    b64url_encode,
    generate_x25519_keypair,
    x25519_exchange,
)
from ..domain.household_dashboard import parse_panel_key

__all__ = [
    "PANEL_CLAIM_HKDF_INFO",
    "PANEL_CLAIM_SUITE_X25519_AESGCM",
    "SUPPORTED_PANEL_CLAIM_SUITES",
    "InvalidPanelKey",
    "OpenedClaim",
    "SealedClaimInvalid",
    "UnsupportedPanelClaimSuite",
    "claim_aad",
    "claim_key",
    "open_sealed_claim",
    "parse_panel_key",
    "seal_claim",
    "seal_claim_with",
]

#: X25519 ECDH → HKDF-SHA256 → AES-256-GCM. Phase 2 of ``docs/crypto.md``
#: adds ``"x25519+mlkem768-hkdf-sha256-aes256gcm"`` as a sibling.
PANEL_CLAIM_SUITE_X25519_AESGCM = "x25519-hkdf-sha256-aes256gcm"
SUPPORTED_PANEL_CLAIM_SUITES: frozenset[str] = frozenset(
    {PANEL_CLAIM_SUITE_X25519_AESGCM}
)
#: HKDF ``info`` — domain-separates this key from every other X25519 use.
PANEL_CLAIM_HKDF_INFO = b"socialhome-panel-claim-v1"

_KEY_BYTES = 32
_NONCE_BYTES = 12
_TAG_BYTES = 16
#: A 32-byte value as unpadded base64url.
_B64_KEY = re.compile(r"[A-Za-z0-9_-]{43}")
_B64_NONCE = re.compile(r"[A-Za-z0-9_-]{16}")
_B64 = re.compile(r"[A-Za-z0-9_-]+")
#: The plaintext is a claim plus a URL — far below this; a bigger
#: ciphertext is refused before any work.
_MAX_CT_BYTES = 2048


class UnsupportedPanelClaimSuite(ValueError):
    """A sealed claim names a suite this build doesn't implement.
    Receivers reject it — never fall back to another suite."""


class InvalidPanelKey(ValueError):
    """The panel's public key isn't a usable X25519 key (malformed, or a
    small-order point that would make the shared secret all zeros)."""


class SealedClaimInvalid(ValueError):
    """A sealed claim didn't open: malformed, sealed to another key, or
    tampered with. Deliberately carries no detail (no error oracle)."""


@dataclass(slots=True, frozen=True)
class OpenedClaim:
    """What a sealed claim carries."""

    claim: str = field(repr=False)
    url: str


def claim_aad(suite: str, panel_pub: bytes) -> bytes:
    """The AEAD's associated data: the suite id (ASCII) then the 32-byte
    panel public key — a ciphertext is bound to both."""
    return suite.encode("ascii") + panel_pub


def claim_key(shared: bytes, epk: bytes, panel_pub: bytes) -> bytes:
    """HKDF-SHA256(``shared``, ``salt = epk || panel_pub``,
    ``info =`` :data:`PANEL_CLAIM_HKDF_INFO`) → the 32-byte AES key."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=epk + panel_pub,
        info=PANEL_CLAIM_HKDF_INFO,
    ).derive(shared)


def _shared(private: bytes, public: bytes) -> bytes:
    try:
        shared = x25519_exchange(private, public)
    except ValueError as exc:
        # ``cryptography`` refuses a small-order peer (all-zero secret).
        raise InvalidPanelKey() from exc
    if shared == bytes(_KEY_BYTES):
        raise InvalidPanelKey()
    return shared


def _plaintext(claim: str, url: str) -> bytes:
    return json.dumps(
        {"claim": claim, "url": url}, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def seal_claim_with(
    *,
    claim: str,
    url: str,
    panel_pub: bytes,
    eph_private: bytes,
    eph_public: bytes,
    nonce: bytes,
) -> str:
    """The deterministic core of :func:`seal_claim`: seal with the given
    ephemeral key and nonce (the shared test vectors pin these). Callers
    other than :func:`seal_claim` and the vector tests must not pick
    their own ephemeral key or nonce."""
    if len(panel_pub) != _KEY_BYTES or len(nonce) != _NONCE_BYTES:
        raise InvalidPanelKey()
    suite = PANEL_CLAIM_SUITE_X25519_AESGCM
    key = claim_key(_shared(eph_private, panel_pub), eph_public, panel_pub)
    ct = AESGCM(key).encrypt(nonce, _plaintext(claim, url), claim_aad(suite, panel_pub))
    return json.dumps(
        {
            "ct": b64url_encode(ct),
            "epk": b64url_encode(eph_public),
            "nonce": b64url_encode(nonce),
            "suite": suite,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def seal_claim(*, claim: str, url: str, panel_pub: bytes) -> str:
    """Seal ``claim`` and ``url`` to the panel key ``panel_pub`` (32 raw
    bytes): a fresh ephemeral X25519 key and a fresh random nonce per
    call, so no (key, nonce) pair ever repeats. Returns the ``sealed``
    action variable. :class:`InvalidPanelKey` for an unusable key."""
    eph = generate_x25519_keypair()
    return seal_claim_with(
        claim=claim,
        url=url,
        panel_pub=panel_pub,
        eph_private=eph.private_key,
        eph_public=eph.public_key,
        nonce=os.urandom(_NONCE_BYTES),
    )


def _field(sealed: dict[str, object], name: str, pattern: re.Pattern[str]) -> bytes:
    value = sealed.get(name)
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise SealedClaimInvalid()
    raw = b64url_decode(value)
    if b64url_encode(raw) != value:
        raise SealedClaimInvalid()
    return raw


def open_sealed_claim(
    sealed: str, *, panel_private: bytes, panel_pub: bytes
) -> OpenedClaim:
    """The panel's side (reference for the firmware): parse ``sealed``,
    reject an unknown suite (:class:`UnsupportedPanelClaimSuite`), derive
    the key and open it. Any other failure — malformed, wrong key,
    tampered — is one :class:`SealedClaimInvalid` with no detail."""
    try:
        parsed = json.loads(sealed)
    except (TypeError, ValueError) as exc:
        raise SealedClaimInvalid() from exc
    if not isinstance(parsed, dict):
        raise SealedClaimInvalid()
    suite = parsed.get("suite")
    if suite not in SUPPORTED_PANEL_CLAIM_SUITES:
        raise UnsupportedPanelClaimSuite(
            f"sealed claim names unsupported suite {suite!r}; this build"
            f" supports {sorted(SUPPORTED_PANEL_CLAIM_SUITES)!r}"
        )
    epk = _field(parsed, "epk", _B64_KEY)
    nonce = _field(parsed, "nonce", _B64_NONCE)
    ct = _field(parsed, "ct", _B64)
    if not _TAG_BYTES < len(ct) <= _MAX_CT_BYTES:
        raise SealedClaimInvalid()
    try:
        key = claim_key(_shared(panel_private, epk), epk, panel_pub)
        plain = AESGCM(key).decrypt(nonce, ct, claim_aad(str(suite), panel_pub))
        body = json.loads(plain)
    except (InvalidPanelKey, InvalidTag, ValueError) as exc:
        raise SealedClaimInvalid() from exc
    if not isinstance(body, dict):
        raise SealedClaimInvalid()
    claim, url = body.get("claim"), body.get("url")
    if not isinstance(claim, str) or not isinstance(url, str):
        raise SealedClaimInvalid()
    return OpenedClaim(claim=claim, url=url)
