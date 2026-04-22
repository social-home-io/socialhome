"""ML-DSA-65 signer / verifier — the post-quantum side of the hybrid suite.

Wraps :mod:`liboqs-python` (``oqs``). Because liboqs ships a native
library, we treat it as an optional extra (see ``pyproject.toml`` entry
``[project.optional-dependencies] pq``). Deployments on the classical
``sig_suite = "ed25519"`` never touch this module's sign / verify paths
and therefore never need liboqs installed.

If :data:`oqs` is ``None`` (extra not installed) calls to :meth:`sign`
and :meth:`verify` / :meth:`generate_keypair` raise
:class:`RuntimeError` with an install hint — a hard fail with a useful
message rather than a silent degrade.

Algorithm: ML-DSA-65 (FIPS 204, NIST security level 3). The 65 variant
is the middle of three parameter sets — level 3 balances signature
size (≈3.3 KB) against verification speed. Level 2 (ML-DSA-44) is
smaller but weaker; level 5 (ML-DSA-87) is overkill for a
household-scale federation.
"""

from __future__ import annotations

try:
    import oqs as _oqs
except ImportError:
    _oqs = None


#: Canonical algorithm name for liboqs lookups. The wire-format short
#: name (``mldsa65`` — used in suite identifiers and the envelope
#: ``signatures`` map) is mapped to this longer liboqs identifier inside
#: the class.
_MLDSA_ALG: str = "ML-DSA-65"


#: User-facing message when the ``pq`` extra isn't installed. Kept
#: module-level so tests can assert on the exact wording.
_OQS_UNAVAILABLE_MSG: str = (
    "federation_sig_suite='ed25519+mldsa65' requires liboqs — "
    "install the optional dependency with: pip install 'socialhome[pq]'"
)


def _require_oqs():
    """Return the ``oqs`` module or raise with an install hint."""
    if _oqs is None:
        raise RuntimeError(_OQS_UNAVAILABLE_MSG)
    return _oqs


class PqSigner:
    """ML-DSA-65 signer bound to a persisted secret key.

    The secret key is passed in the constructor and never leaves this
    object (no getter). Sign / verify operations open a fresh
    ``oqs.Signature`` context per call — the liboqs binding is not
    thread-safe when a single signer object is reused across threads.
    """

    __slots__ = ("_secret_key",)

    def __init__(self, secret_key: bytes) -> None:
        self._secret_key = secret_key

    def sign(self, message: bytes) -> bytes:
        """Produce an ML-DSA-65 signature over ``message``."""
        oqs = _require_oqs()
        with oqs.Signature(_MLDSA_ALG, self._secret_key) as sig:
            return bytes(sig.sign(message))

    @staticmethod
    def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
        """Return ``True`` iff ``signature`` is a valid ML-DSA-65
        signature over ``message`` under ``public_key``.

        Never raises — any liboqs exception (malformed input, wrong
        algorithm parameters, empty bytes) becomes ``False``.
        """
        try:
            oqs = _require_oqs()
        except RuntimeError:
            # Caller asked for PQ verification but liboqs isn't installed.
            # Return False rather than raise — this mirrors
            # :func:`~socialhome.crypto.verify_ed25519` which swallows
            # cryptography-library exceptions into a boolean result.
            return False
        try:
            with oqs.Signature(_MLDSA_ALG) as verifier:
                return bool(verifier.verify(message, signature, public_key))
        except Exception:
            return False

    @staticmethod
    def generate_keypair() -> tuple[bytes, bytes]:
        """Mint a fresh ``(secret_key, public_key)`` tuple."""
        oqs = _require_oqs()
        with oqs.Signature(_MLDSA_ALG) as sig:
            pk = bytes(sig.generate_keypair())
            sk = bytes(sig.export_secret_key())
        return sk, pk


def is_available() -> bool:
    """Return ``True`` iff liboqs is importable in this process."""
    return _oqs is not None


__all__ = [
    "PqSigner",
    "is_available",
]
