"""Federation signature-suite registry.

A *suite* identifies which algorithms a federation envelope is signed
with. It appears on the wire as the ``sig_suite`` envelope field and is
persisted per-peer on :attr:`RemoteInstance.sig_suite`.

Grammar: one or more algorithm identifiers joined by ``+``. Example
suites:

* ``"ed25519"`` — classical, single Ed25519 signature.
* ``"ed25519+mldsa65"`` — hybrid: Ed25519 **AND** ML-DSA-65 (FIPS 204).
  Verification requires *both* signatures to pass, never either/or —
  an attacker must break both algorithms, not just one.

Adding a new algorithm (e.g. SLH-DSA) is a three-step change:

1. Add the string identifier to :data:`KNOWN_ALGORITHMS`.
2. Add a suite identifier (e.g. ``"ed25519+slhdsa"``) to
   :data:`SUPPORTED_SUITES`.
3. Extend :class:`FederationEncoder` to sign / verify with the new
   algorithm and wire a signer class similar to
   :class:`~social_home.federation.pq_signer.PqSigner`.

This module is deliberately data-only — no imports from crypto
libraries, no runtime I/O. It documents the contract that the encoder
and validator dispatch off.
"""

from __future__ import annotations

#: Every individual algorithm identifier the suite grammar recognises.
#: Adding a new entry here does NOT enable the algorithm — the encoder
#: must also grow a signer for it. Wire-format identifiers should stay
#: short, lowercase, no punctuation beyond digits.
KNOWN_ALGORITHMS: frozenset[str] = frozenset(
    {
        "ed25519",
        "mldsa65",  # ML-DSA-65 (FIPS 204); NIST security level 3.
    }
)

#: Suites this deployment recognises on the wire. The default suite
#: (first listed) is used when no per-peer override is set. Keep the
#: list ordered from "safest for the longest" to "most minimal".
SUPPORTED_SUITES: tuple[str, ...] = (
    "ed25519",
    "ed25519+mldsa65",
)

#: The classical suite. Every peer must support it as a fallback, even
#: if the hybrid suite is the preferred wire format.
DEFAULT_SUITE: str = "ed25519"


def parse_suite(suite: str) -> tuple[str, ...]:
    """Split a suite identifier into its component algorithms.

    Returns the algorithms in declaration order (i.e. exactly as they
    appear in the suite string). Does not validate that each algorithm
    is in :data:`KNOWN_ALGORITHMS` — callers that care (encoder /
    validator) do the allow-list check themselves.

    Raises :class:`ValueError` on an empty suite string.
    """
    if not suite:
        raise ValueError("sig_suite must not be empty")
    return tuple(suite.split("+"))


def validate_suite(suite: str) -> None:
    """Check that ``suite`` is in :data:`SUPPORTED_SUITES`.

    Raises :class:`ValueError` otherwise. Used by the pairing
    coordinator to reject handshake payloads advertising an unknown
    suite.
    """
    if suite not in SUPPORTED_SUITES:
        raise ValueError(
            f"sig_suite={suite!r} not recognised; expected one of {SUPPORTED_SUITES}",
        )


def negotiate(local_suite: str, remote_suite: str) -> str:
    """Return the strongest suite both peers support.

    Negotiation rule for pairing: if both peers advertise a hybrid
    suite, use it; otherwise fall back to the classical default. For
    v1 this is a simple intersection of two fixed strings — if the
    registry grows more suites, this becomes a rank-and-pick.
    """
    if local_suite == remote_suite and local_suite in SUPPORTED_SUITES:
        return local_suite
    # Heterogeneous pair: one side is classical, one is hybrid → classical.
    return DEFAULT_SUITE


__all__ = [
    "DEFAULT_SUITE",
    "KNOWN_ALGORITHMS",
    "SUPPORTED_SUITES",
    "negotiate",
    "parse_suite",
    "validate_suite",
]
