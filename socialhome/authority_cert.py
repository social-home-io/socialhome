"""Space-authority key certificates — pure, dependency-light (v_44).

A space's authority key (``spaces.identity_public_key``, the Ed25519 key
every space-authority signature verifies against — see
:mod:`socialhome.authority_sig`) used to be permanent: once a delegated
admin household held the seed, it stayed a co-authority forever. v_44
rotates that key whenever a household stops being an admin household, and
the rotation has to be authorized by something the demoted household does
NOT hold — so it is signed by the OWNER HOUSEHOLD's identity key, never by
the old space key.

The certificate says exactly one thing: "space ``space_id`` is now governed
by authority key ``authority_pk`` at epoch ``key_epoch``", signed by the
owner household. It names no member, no reason, no revoked household, so a
relay or a connection server that sees one learns only that a rotation
happened (and how many).

Receivers verify it with keys they ALREADY hold — no new registry:

* ``derive_instance_id(owner_pk) == owner_instance_id`` — an instance id IS
  the hash of its identity key, so a cert can only be produced by the
  household that owns the space;
* optionally ``owner_pk`` equals a stored copy of the owner's identity key
  (``remote_instances`` / ``host_identity_pk`` on a member, the registered
  ``client_instances.public_key`` on the GFS).

Like :mod:`socialhome.authority_sig` this module depends only on
:mod:`socialhome.crypto`, so the content-blind GFS process can import it.

Wire shape (every field always present, never positional)::

    {"space_id", "owner_instance_id", "owner_pk", "authority_pk",
     "authority_key_suite": "ed25519", "key_epoch": N, "issued_at",
     "cert_sig_suite": "ed25519", "cert_sig"}

Signing bytes: ``b"space-authority-cert:v1:" + canonical_json(cert minus
cert_sig)`` — sorted keys, compact separators, so both suite tags are inside
the signature and cannot be swapped in transit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from .crypto import (
    b64url_decode,
    b64url_encode,
    derive_instance_id,
    sign_ed25519,
    verify_ed25519,
)

#: Suite of the owner-household signature over the cert. Phase-2 of
#: ``docs/crypto.md`` adds ``"ed25519+mldsa65"`` as a sibling.
AUTHORITY_CERT_SUITE_ED25519: str = "ed25519"
SUPPORTED_AUTHORITY_CERT_SUITES: frozenset[str] = frozenset(
    {AUTHORITY_CERT_SUITE_ED25519}
)

#: Suite of the certified authority key itself — what kind of key
#: ``authority_pk`` is. Kept separate from the cert signature suite: the
#: owner's identity key and the space key migrate independently.
AUTHORITY_KEY_SUITE_ED25519: str = "ed25519"
SUPPORTED_AUTHORITY_KEY_SUITES: frozenset[str] = frozenset(
    {AUTHORITY_KEY_SUITE_ED25519}
)

#: Largest ``key_epoch`` a cert may carry — it must fit a signed 64-bit
#: SQLite INTEGER on every receiver. Anything larger is refused.
MAX_AUTHORITY_KEY_EPOCH: int = 2**63 - 1

#: Domain-separation prefix of the signing bytes.
_CERT_PREFIX = b"space-authority-cert:v1:"

#: Every key a cert carries. A cert with anything missing is malformed.
AUTHORITY_CERT_FIELDS: frozenset[str] = frozenset(
    {
        "space_id",
        "owner_instance_id",
        "owner_pk",
        "authority_pk",
        "authority_key_suite",
        "key_epoch",
        "issued_at",
        "cert_sig_suite",
        "cert_sig",
    }
)


class UnsupportedAuthorityCertSuite(ValueError):
    """The cert's signature suite or key suite is unknown to this build.

    Receivers MUST reject — there is no default fallback (crypto-suite
    rule), otherwise a downgrade is possible.
    """


class InvalidAuthorityCert(ValueError):
    """The cert is malformed, bound to another space / owner, or its
    signature does not verify."""


@dataclass(slots=True, frozen=True)
class VerifiedAuthorityCert:
    """The facts a verified cert establishes. Never constructed for a
    cert that failed any check."""

    space_id: str
    owner_instance_id: str
    owner_pk_hex: str
    authority_pk_hex: str
    key_epoch: int


def authority_cert_signing_bytes(cert: dict) -> bytes:
    """Canonical, domain-separated bytes the owner signs: the cert without
    ``cert_sig``, sorted keys, compact separators."""
    body = {k: v for k, v in cert.items() if k != "cert_sig"}
    return _CERT_PREFIX + json.dumps(
        body, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def sign_authority_cert(
    *,
    space_id: str,
    owner_instance_id: str,
    owner_seed: bytes,
    owner_pk_hex: str,
    authority_pk_hex: str,
    key_epoch: int,
    issued_at: str | None = None,
) -> dict:
    """Build and sign a cert with the owner household's identity seed.

    ``key_epoch`` starts at 1: epoch 0 is the creation-time key, which is
    TOFU-pinned and never needs a cert.
    """
    if isinstance(key_epoch, bool) or not isinstance(key_epoch, int):
        raise ValueError("key_epoch must be an int")
    if key_epoch < 1 or key_epoch > MAX_AUTHORITY_KEY_EPOCH:
        raise ValueError("key_epoch must be in 1..2**63-1")
    cert: dict = {
        "space_id": space_id,
        "owner_instance_id": owner_instance_id,
        "owner_pk": owner_pk_hex,
        "authority_pk": authority_pk_hex,
        "authority_key_suite": AUTHORITY_KEY_SUITE_ED25519,
        "key_epoch": key_epoch,
        "issued_at": issued_at or datetime.now(timezone.utc).isoformat(),
        "cert_sig_suite": AUTHORITY_CERT_SUITE_ED25519,
    }
    cert["cert_sig"] = b64url_encode(
        sign_ed25519(owner_seed, authority_cert_signing_bytes(cert))
    )
    return cert


def _hex32(value: object, field: str) -> bytes:
    if not isinstance(value, str):
        raise InvalidAuthorityCert(f"{field} is not a string")
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise InvalidAuthorityCert(f"{field} is not hex") from exc
    if len(raw) != 32:
        raise InvalidAuthorityCert(f"{field} is not 32 bytes")
    return raw


def verify_authority_cert(
    cert: object,
    *,
    space_id: str,
    owner_instance_id: str,
    known_owner_pk_hex: str | None = None,
) -> VerifiedAuthorityCert:
    """Verify ``cert`` for ``space_id`` owned by ``owner_instance_id``.

    Raises :class:`UnsupportedAuthorityCertSuite` for an unknown or missing
    suite (no default), :class:`InvalidAuthorityCert` for every other
    failure. Epoch ordering (newer than what the receiver holds) is the
    caller's decision — this only proves the owner issued the cert.
    """
    if not isinstance(cert, dict):
        raise InvalidAuthorityCert("cert is not an object")
    if cert.get("cert_sig_suite") not in SUPPORTED_AUTHORITY_CERT_SUITES:
        raise UnsupportedAuthorityCertSuite(str(cert.get("cert_sig_suite")))
    if cert.get("authority_key_suite") not in SUPPORTED_AUTHORITY_KEY_SUITES:
        raise UnsupportedAuthorityCertSuite(str(cert.get("authority_key_suite")))
    missing = AUTHORITY_CERT_FIELDS - set(cert)
    if missing:
        raise InvalidAuthorityCert(f"cert is missing {sorted(missing)}")
    if cert["space_id"] != space_id:
        raise InvalidAuthorityCert("cert is for another space")
    if not owner_instance_id or cert["owner_instance_id"] != owner_instance_id:
        raise InvalidAuthorityCert("cert names another owner")
    owner_pk = _hex32(cert["owner_pk"], "owner_pk")
    _hex32(cert["authority_pk"], "authority_pk")
    if derive_instance_id(owner_pk) != owner_instance_id:
        raise InvalidAuthorityCert("owner_pk does not derive to the owner id")
    if known_owner_pk_hex and cert["owner_pk"].lower() != known_owner_pk_hex.lower():
        raise InvalidAuthorityCert("owner_pk differs from the stored owner key")
    epoch = cert["key_epoch"]
    if (
        isinstance(epoch, bool)
        or not isinstance(epoch, int)
        or epoch < 1
        or epoch > MAX_AUTHORITY_KEY_EPOCH
    ):
        raise InvalidAuthorityCert("key_epoch must be an int in 1..2**63-1")
    if not isinstance(cert["issued_at"], str):
        raise InvalidAuthorityCert("issued_at is not a string")
    sig_text = cert["cert_sig"]
    if not isinstance(sig_text, str):
        raise InvalidAuthorityCert("cert_sig is not a string")
    try:
        sig = b64url_decode(sig_text)
    except Exception as exc:
        raise InvalidAuthorityCert("cert_sig is not base64url") from exc
    if not verify_ed25519(owner_pk, authority_cert_signing_bytes(cert), sig):
        raise InvalidAuthorityCert("cert signature does not verify")
    return VerifiedAuthorityCert(
        space_id=space_id,
        owner_instance_id=owner_instance_id,
        owner_pk_hex=cert["owner_pk"].lower(),
        authority_pk_hex=cert["authority_pk"].lower(),
        key_epoch=epoch,
    )


def authority_cert_epoch(cert: object) -> int:
    """``key_epoch`` of an already-verified, stored cert; ``0`` for none.

    For ordering only (the GFS compares an offered cert against the one it
    stored after verifying it). Never a substitute for
    :func:`verify_authority_cert`.
    """
    if not isinstance(cert, dict):
        return 0
    epoch = cert.get("key_epoch")
    if (
        isinstance(epoch, bool)
        or not isinstance(epoch, int)
        or epoch < 1
        or epoch > MAX_AUTHORITY_KEY_EPOCH
    ):
        return 0
    return epoch
