"""Signed GFS capability block — pure, low-level, dependency-light.

``GET /gfs/info`` is the capability channel for the GFS↔HFS leg (that leg has
no ``proto_version`` negotiation), and the capability that matters is
``anonymous_publish``: it tells a household that ``POST /gfs/publish``
authorizes purely on the space-authority signature inside the opaque payload,
so the household can stop sending ``from_instance`` plus its own transport
signature.

A BARE flag on an unauthenticated endpoint is not enough. An on-path attacker
(or a GFS reachable over plain ``http://``) could simply strip the flag and
every household would "safely" fall back to the legacy identified body — the
body that carries a household-signed, third-party-provable "household X
relayed into space Y" artefact. The fallback is the exact harm the anonymous
relay exists to prevent, so the downgrade must be authenticated away: the GFS
signs its capability block with its OWN identity key, whose public half the
household pinned (TOFU) at pair time, and the household trusts the capability
only when that signature verifies.

This module sits at the package top level (beside :mod:`socialhome.authority_sig`,
same discipline) and depends ONLY on :mod:`socialhome.crypto` (Ed25519
sign/verify + base64url helpers) so BOTH sides can import it: the content-blind
GFS process signs with it, the HFS
:mod:`socialhome.services.gfs_connection_service` verifies with it, and neither
drags the other's stack in — in particular the household never imports the
``global_server`` package to read a capability block.
"""

from __future__ import annotations

import json

from .crypto import (
    b64url_decode,
    b64url_encode,
    sign_ed25519,
    verify_ed25519,
)

#: Suite identifier for the capability-block signature, shipped on the wire
#: next to the signature so the algorithm can swap without breaking older
#: receivers (crypto-suite rule). Today Ed25519; Phase-2 of ``docs/crypto.md``
#: introduces a sibling ``"ed25519+mldsa65"`` (parallel PQ signature).
#: Receivers MUST reject any suite not in :data:`SUPPORTED_CAPS_SIG_SUITES` —
#: never fall back to a default, or the downgrade this block exists to stop
#: comes back through the suite field.
CAPS_SIG_SUITE_ED25519: str = "ed25519"
SUPPORTED_CAPS_SIG_SUITES: frozenset[str] = frozenset({CAPS_SIG_SUITE_ED25519})


class UnsupportedCapsSigSuite(ValueError):
    """Raised when a capability block advertises a suite this build doesn't
    know. Receivers MUST reject rather than fall back to a default."""


#: Versioned domain-separation prefix. Keeps a capability signature from being
#: lifted onto any other statement the GFS identity key signs (cluster sync
#: frames, future descriptors) and vice-versa.
CAPS_SIGNING_PREFIX: bytes = b"gfs-capabilities:v1:"


def capabilities_signing_bytes(gfs_instance_id: str, capabilities: dict) -> bytes:
    """Canonical, domain-separated message bytes for a capability block.

    Binds the capability map to the GFS instance id that advertises it, so a
    block served by GFS A cannot be replayed by GFS B. ``sort_keys`` + compact
    separators make the encoding canonical: a receiver that re-serialises the
    parsed JSON reproduces the signer's exact bytes regardless of key order.
    """
    body = json.dumps(
        {"gfs_instance_id": gfs_instance_id, "capabilities": capabilities},
        separators=(",", ":"),
        sort_keys=True,
    )
    return CAPS_SIGNING_PREFIX + body.encode("utf-8")


def sign_capabilities(
    seed: bytes,
    gfs_instance_id: str,
    capabilities: dict,
) -> tuple[str, str]:
    """Sign a capability block with the GFS's Ed25519 identity seed.

    Returns ``(signature_b64url, suite)`` — the two wire fields
    ``capabilities_sig`` / ``capabilities_sig_suite``. The matching public key
    is the one already published as ``public_key`` on the same ``/gfs/info``
    response and pinned by every paired household; no new key is minted.
    """
    sig = sign_ed25519(
        seed,
        capabilities_signing_bytes(gfs_instance_id, capabilities),
    )
    return b64url_encode(sig), CAPS_SIG_SUITE_ED25519


def verify_capabilities(
    public_key_hex: str,
    gfs_instance_id: str,
    capabilities: dict,
    sig_b64url: str,
    suite: str,
) -> bool:
    """Verify a capability block against the household's PINNED GFS key.

    Raises :class:`UnsupportedCapsSigSuite` for an unknown suite (no default
    fallback). Returns ``False`` — never raises — for malformed hex / base64
    material or a failed verification: every input here is remote-controlled
    and the caller runs inside a best-effort fetch that must not blow up.
    """
    if suite not in SUPPORTED_CAPS_SIG_SUITES:
        raise UnsupportedCapsSigSuite(suite)
    try:
        public_key = bytes.fromhex(public_key_hex)
        sig = b64url_decode(sig_b64url)
    except Exception:
        return False
    return verify_ed25519(
        public_key,
        capabilities_signing_bytes(gfs_instance_id, capabilities),
        sig,
    )
