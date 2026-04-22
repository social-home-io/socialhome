"""Federation envelope encoder — encrypt / decrypt / sign / verify.

Extracted from :class:`FederationService` so the crypto surface can be
unit-tested in isolation and so future transports (WebRTC, Tor) reuse
the same primitives without depending on the federation service's
state (http client, replay cache, repos).

Signature layer: every envelope carries a ``sig_suite`` identifier and
a ``signatures`` map keyed by algorithm name. The classical suite
``"ed25519"`` populates the map with a single ``ed25519`` entry; the
hybrid suite ``"ed25519+mldsa65"`` populates both ``ed25519`` and
``mldsa65`` entries. Verification requires **every** algorithm named in
``sig_suite`` to validate — never either/or — so an attacker must break
every algorithm in the suite, not just one.

Payload layer: the encrypted payload stays AES-256-GCM regardless of
suite. AES-256 is Grover-resistant (effective ~128-bit security post-
quantum); the PQ story is purely about the signature layer.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..crypto import b64url_decode, b64url_encode, sign_ed25519, verify_ed25519
from .crypto_suite import parse_suite
from .pq_signer import PqSigner


class FederationEncoder:
    """AES-256-GCM payload crypto + multi-algorithm envelope signing.

    Parameters
    ----------
    own_identity_seed:
        32-byte Ed25519 seed for signing outbound envelopes. The matching
        public key lives in ``instance_identity.identity_public_key`` and
        is advertised to peers during pairing.
    pq_signer:
        Optional post-quantum signer (see
        :class:`~socialhome.federation.pq_signer.PqSigner`). When
        ``None``, only the ``ed25519`` entry is populated in outbound
        ``signatures`` maps.
    sig_suite:
        Default wire suite to use when the caller doesn't supply a
        per-peer override. The per-peer suite on
        :attr:`RemoteInstance.sig_suite` always wins when present.
    """

    __slots__ = ("_own_identity_seed", "_pq_signer", "_sig_suite")

    def __init__(
        self,
        own_identity_seed: bytes,
        *,
        pq_signer=None,
        sig_suite: str = "ed25519",
    ) -> None:
        self._own_identity_seed = own_identity_seed
        self._pq_signer = pq_signer
        self._sig_suite = sig_suite

    @property
    def sig_suite(self) -> str:
        """Default suite this encoder emits when no per-peer override."""
        return self._sig_suite

    @property
    def has_pq(self) -> bool:
        """True when this encoder was configured with a PQ signer."""
        return self._pq_signer is not None

    # ─── AES-256-GCM payloads ────────────────────────────────────────────

    def encrypt_payload(self, payload_json: str, session_key: bytes) -> str:
        """AES-256-GCM encrypt → ``b64url(nonce):b64url(ciphertext:tag)``."""
        nonce = os.urandom(12)
        aesgcm = AESGCM(session_key)
        ct_with_tag = aesgcm.encrypt(nonce, payload_json.encode("utf-8"), None)
        return b64url_encode(nonce) + ":" + b64url_encode(ct_with_tag)

    def decrypt_payload(self, encrypted: str, session_key: bytes) -> str:
        """Inverse of :meth:`encrypt_payload`.

        Raises :class:`ValueError` if the wire format is wrong, or
        :class:`cryptography.exceptions.InvalidTag` if the GCM tag fails.
        """
        try:
            nonce_b64, ct_b64 = encrypted.split(":", 1)
        except ValueError as exc:
            raise ValueError("Malformed encrypted payload format") from exc
        nonce = b64url_decode(nonce_b64)
        ct_with_tag = b64url_decode(ct_b64)
        aesgcm = AESGCM(session_key)
        plaintext = aesgcm.decrypt(nonce, ct_with_tag, None)
        return plaintext.decode("utf-8")

    # ─── Multi-algorithm envelope signatures ─────────────────────────────

    def sign_envelope(self, envelope_bytes: bytes) -> str:
        """Ed25519-only convenience signer — returns b64url signature.

        Retained for callers that want a single string (internal tests,
        SDP signing). Prefer :meth:`sign_envelope_all` for outbound
        federation envelopes — it emits the full suite-aware map.
        """
        sig = sign_ed25519(self._own_identity_seed, envelope_bytes)
        return b64url_encode(sig)

    def sign_envelope_all(
        self,
        envelope_bytes: bytes,
        *,
        suite: str | None = None,
    ) -> dict[str, str]:
        """Sign ``envelope_bytes`` with every algorithm in ``suite``.

        Returns a dict keyed by algorithm identifier (``"ed25519"``,
        ``"mldsa65"``) whose values are b64url signature strings.
        Raises :class:`RuntimeError` if the suite includes ``mldsa65``
        and this encoder was constructed without a PQ signer.
        """
        effective_suite = suite or self._sig_suite
        algorithms = parse_suite(effective_suite)
        signatures: dict[str, str] = {}
        for algo in algorithms:
            if algo == "ed25519":
                sig = sign_ed25519(self._own_identity_seed, envelope_bytes)
                signatures["ed25519"] = b64url_encode(sig)
            elif algo == "mldsa65":
                if self._pq_signer is None:
                    raise RuntimeError(
                        "sig_suite includes 'mldsa65' but no PQ signer is"
                        " attached — did you install the 'pq' extra and set"
                        " config.federation_sig_suite?"
                    )
                sig = self._pq_signer.sign(envelope_bytes)
                signatures["mldsa65"] = b64url_encode(sig)
            else:
                raise ValueError(f"sig_suite algorithm {algo!r} has no signer wired")
        return signatures

    def verify_signature(
        self,
        envelope_bytes: bytes,
        signature: str,
        public_key: bytes,
    ) -> bool:
        """Ed25519 verify ``signature`` over ``envelope_bytes`` with ``public_key``."""
        try:
            sig_bytes = b64url_decode(signature)
        except Exception:
            return False
        return verify_ed25519(public_key, envelope_bytes, sig_bytes)

    def verify_signatures_all(
        self,
        envelope_bytes: bytes,
        *,
        suite: str,
        signatures: dict[str, str],
        ed_public_key: bytes,
        pq_public_key: bytes | None,
    ) -> bool:
        """Verify every signature the suite requires.

        Returns ``True`` iff the ``signatures`` map's keys are exactly
        the algorithms named in ``suite`` AND every signature validates
        against the matching public key. Missing algorithms, extra
        algorithms, or a single failed signature all return ``False``.

        ``pq_public_key`` is required when the suite contains a PQ
        algorithm; pass the raw ML-DSA-65 public key bytes. When
        ``suite == "ed25519"`` it is ignored.
        """
        try:
            algorithms = parse_suite(suite)
        except ValueError:
            return False
        if set(algorithms) != set(signatures):
            return False

        for algo in algorithms:
            sig_b64 = signatures[algo]
            try:
                sig_bytes = b64url_decode(sig_b64)
            except Exception:
                return False
            if algo == "ed25519":
                if not verify_ed25519(ed_public_key, envelope_bytes, sig_bytes):
                    return False
            elif algo == "mldsa65":
                if pq_public_key is None:
                    return False
                if not PqSigner.verify(
                    pq_public_key,
                    envelope_bytes,
                    sig_bytes,
                ):
                    return False
            else:
                return False
        return True
