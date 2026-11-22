"""A pending household asks its GFS whether the operator approved it.

A household connecting to a GFS whose operator reviews new households
(``auto_accept_clients`` off) is registered ``pending``. The GFS sends no
approval signal — it holds no household address, and it refuses a pending
household's WebSocket — so the household asks, riding the challenge it
already sends (``POST /gfs/proof``, :mod:`socialhome.gfs_proof`):

* **The question is signed by the household.** It adds ``client:
  {instance_id, sig, sig_suite}`` to the proof body, ``sig`` over
  ``b"gfs-client-status-request:v1:" + canonical({gfs_key, instance_id,
  nonce, suite})`` with its identity key — the key it registered. Only the
  key holder can get a real answer, so the endpoint is no oracle for which
  instance ids are registered (the same rule as the WebSocket hello). The
  question names the key of the GFS it is meant for (the one this household
  pinned): another GFS that saw it can't replay it elsewhere to learn whether
  — or how — this household is registered there.
* **The answer is signed by the GFS.** The GFS answers ``client_status`` —
  ``active`` / ``pending`` / ``banned`` for a verified question, ``unknown``
  for every unknown instance or bad signature alike (it verifies against a
  fixed dummy key for an unknown one, so the two take the same work) — with
  ``client_status_sig`` over ``b"gfs-client-status:v1:" + canonical({gfs_key,
  instance_id, nonce, request_sig, status, suite})`` with its identity key.
  The household verifies it against the key it PINNED for that connection,
  so nobody else can fake an approval; the fresh nonce makes every answer
  single-use, and ``request_sig`` — the question's signature as the GFS
  received it — binds the answer to this exact question, so nobody on the
  path can swap in a broken question and get a signed ``unknown`` back.

No URL is bound: a relay that forwards the question to the real GFS gets
back the real GFS's answer, which is the truth. No timestamp either — the
household's nonce already makes every answer fresh, and a skewed clock must
never read as a rejection.

Dependency-light (only :mod:`socialhome.crypto` and the canonical JSON) so the
GFS process and the household share it, like :mod:`.gfs_proof`.
"""

from __future__ import annotations

import binascii

from .canonical_json import canonical_str
from .crypto import (
    b64url_decode,
    b64url_encode,
    ed25519_public_key,
    sign_ed25519,
    verify_ed25519,
)

#: Suite identifier for both signatures (the household's question and the
#: GFS's answer), shipped as ``sig_suite`` / ``client_status_sig_suite`` and
#: bound into the signed bytes (crypto-suite rule).
CLIENT_STATUS_SIG_SUITE_ED25519: str = "ed25519"
SUPPORTED_CLIENT_STATUS_SIG_SUITES: frozenset[str] = frozenset(
    {CLIENT_STATUS_SIG_SUITE_ED25519}
)


class UnsupportedClientStatusSigSuite(ValueError):
    """Raised when a question or answer names a suite this build doesn't
    know. Receivers MUST reject rather than fall back to a default."""


#: Versioned domain-separation prefixes — never confusable with each other,
#: with a possession proof (``gfs-proof:v1:``) or a capability block.
CLIENT_STATUS_REQUEST_PREFIX: bytes = b"gfs-client-status-request:v1:"
CLIENT_STATUS_SIGNING_PREFIX: bytes = b"gfs-client-status:v1:"

#: What a GFS answers. ``unknown`` covers an instance it does not hold (the
#: operator rejected or removed it) and a question it could not verify.
CLIENT_STATUSES: frozenset[str] = frozenset({"active", "pending", "banned", "unknown"})

_MAX_INSTANCE_ID_CHARS = 256
#: An Ed25519 signature is 64 bytes → 86 b64url chars.
_MAX_SIG_CHARS = 128
_MAX_SUITE_CHARS = 64


def status_request_signing_bytes(
    gfs_key_hex: str, instance_id: str, nonce: str, suite: str
) -> bytes:
    """Canonical, domain-separated bytes the household signs to ask the GFS
    holding *gfs_key_hex*."""
    body = canonical_str(
        {
            "gfs_key": gfs_key_hex.strip().lower(),
            "instance_id": instance_id,
            "nonce": nonce,
            "suite": suite,
        }
    )
    return CLIENT_STATUS_REQUEST_PREFIX + body.encode("utf-8")


def sign_status_request(
    seed: bytes, gfs_key_hex: str, instance_id: str, nonce: str
) -> dict[str, str]:
    """The ``client`` block a household adds to its ``/gfs/proof`` body,
    for the GFS whose key it pinned (*gfs_key_hex*)."""
    suite = CLIENT_STATUS_SIG_SUITE_ED25519
    message = status_request_signing_bytes(gfs_key_hex, instance_id, nonce, suite)
    sig = b64url_encode(sign_ed25519(seed, message))
    return {"instance_id": instance_id, "sig": sig, "sig_suite": suite}


def parse_status_request(value: object) -> tuple[str, str, str] | None:
    """``(instance_id, sig, sig_suite)`` of a well-formed ``client`` block,
    else ``None``. Shape only — the signature is checked by the caller."""
    if not isinstance(value, dict):
        return None
    instance_id = value.get("instance_id")
    sig = value.get("sig")
    suite = value.get("sig_suite")
    if not (
        isinstance(instance_id, str) and isinstance(sig, str) and isinstance(suite, str)
    ):
        return None
    if not instance_id or len(instance_id) > _MAX_INSTANCE_ID_CHARS:
        return None
    if not sig or len(sig) > _MAX_SIG_CHARS:
        return None
    if not suite or len(suite) > _MAX_SUITE_CHARS:
        return None
    return instance_id, sig, suite


def verify_status_request(
    public_key_hex: str,
    gfs_key_hex: str,
    instance_id: str,
    nonce: str,
    sig_b64url: str,
    suite: str,
) -> bool:
    """Verify a household's question against its registered key, as a
    question for the GFS holding *gfs_key_hex* (the verifier's own key).

    Raises :class:`UnsupportedClientStatusSigSuite` for an unknown suite.
    Returns ``False`` — never raises — for malformed key or signature material.
    """
    if suite not in SUPPORTED_CLIENT_STATUS_SIG_SUITES:
        raise UnsupportedClientStatusSigSuite(suite)
    return _verify(
        public_key_hex,
        status_request_signing_bytes(gfs_key_hex, instance_id, nonce, suite),
        sig_b64url,
    )


def client_status_signing_bytes(
    gfs_key_hex: str,
    instance_id: str,
    nonce: str,
    request_sig: str,
    status: str,
    suite: str,
) -> bytes:
    """Canonical, domain-separated bytes the GFS signs to answer the question
    whose signature was *request_sig*."""
    body = canonical_str(
        {
            "gfs_key": gfs_key_hex.strip().lower(),
            "instance_id": instance_id,
            "nonce": nonce,
            "request_sig": request_sig,
            "status": status,
            "suite": suite,
        }
    )
    return CLIENT_STATUS_SIGNING_PREFIX + body.encode("utf-8")


def sign_client_status(
    seed: bytes, instance_id: str, nonce: str, request_sig: str, status: str
) -> tuple[str, str]:
    """Sign the GFS's answer with its identity seed →
    ``(client_status_sig, client_status_sig_suite)``."""
    if status not in CLIENT_STATUSES:
        raise ValueError(f"unknown client status {status!r}")
    suite = CLIENT_STATUS_SIG_SUITE_ED25519
    gfs_key_hex = ed25519_public_key(seed).hex()
    message = client_status_signing_bytes(
        gfs_key_hex, instance_id, nonce, request_sig, status, suite
    )
    return b64url_encode(sign_ed25519(seed, message)), suite


def verify_client_status(
    gfs_key_hex: str,
    instance_id: str,
    nonce: str,
    request_sig: str,
    status: str,
    sig_b64url: str,
    suite: str,
) -> bool:
    """Verify a GFS's answer against the key the household PINNED, as the
    answer to its own question (*request_sig*).

    Raises :class:`UnsupportedClientStatusSigSuite` for an unknown suite.
    Returns ``False`` — never raises — for a status outside
    :data:`CLIENT_STATUSES`, malformed material or a failed verification:
    every input here is remote-controlled.
    """
    if suite not in SUPPORTED_CLIENT_STATUS_SIG_SUITES:
        raise UnsupportedClientStatusSigSuite(suite)
    if status not in CLIENT_STATUSES:
        return False
    message = client_status_signing_bytes(
        gfs_key_hex, instance_id, nonce, request_sig, status, suite
    )
    return _verify(gfs_key_hex, message, sig_b64url)


def _verify(public_key_hex: str, message: bytes, sig_b64url: str) -> bool:
    try:
        public_key = bytes.fromhex(public_key_hex)
        sig = b64url_decode(sig_b64url)
    except binascii.Error, ValueError, TypeError:
        return False
    if not sig:
        return False
    return verify_ed25519(public_key, message, sig)
