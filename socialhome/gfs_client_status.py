"""A pending household asks its GFS whether the operator approved it.

A household connecting to a GFS whose operator reviews new households
(``auto_accept_clients`` off) is registered ``pending``. The GFS sends no
approval signal — it holds no household address, and it refuses a pending
household's WebSocket — so the household asks, riding the challenge it
already sends (``POST /gfs/proof``, :mod:`socialhome.gfs_proof`):

* **The question is signed by the household.** It adds ``client:
  {instance_id, sig, sig_suite, ts}`` to the proof body, ``sig`` over
  ``b"gfs-client-status-request:v1:" + canonical({gfs_key, instance_id,
  nonce, suite, ts})`` with its identity key — the key it registered. Only
  the key holder can get a real answer, so the endpoint is no oracle for
  which instance ids are registered (the same rule as the WebSocket hello).
  The question names the key of the GFS it is meant for (the one this
  household pinned): another GFS that saw it can't replay it elsewhere to
  learn whether — or how — this household is registered there.
* **A question expires.** ``ts`` is the household's clock floored to the
  minute (:func:`status_request_ts` — no finer reading leaves the house).
  The GFS refuses one older than :data:`CLIENT_STATUS_REQUEST_MAX_AGE_S` or
  further ahead than :data:`CLIENT_STATUS_REQUEST_MAX_FUTURE_S` with an
  UNSIGNED ``400``, like every other malformed question
  (:func:`parse_status_request`: an unsupported suite, a signature that is
  not 64 bytes). The household reads any non-200 as "ask again later", so a
  skewed clock never reads as a rejection — and a question captured on the
  path is no lasting oracle for the household's status.
* **The answer is signed by the GFS and binds the whole question.** The GFS
  answers ``client_status`` — ``active`` / ``pending`` / ``banned`` for a
  verified question, ``unknown`` for an unknown instance or a signature that
  does not verify alike (it verifies against a fixed dummy key for an
  unknown one, so the two take the same work) — with ``client_status_sig``
  over ``b"gfs-client-status:v1:" + canonical({gfs_key, instance_id, nonce,
  request_sig, request_suite, request_ts, status, suite})`` with its
  identity key. The household verifies it against the key it PINNED for
  that connection and against ITS OWN question, so nobody else can fake an
  approval, the fresh nonce makes every answer single-use, and nobody on the
  path can alter any part of the question (its signature, suite or time) and
  get a signed ``unknown`` the household would accept.

No URL is bound: a relay that forwards the question to the real GFS gets
back the real GFS's answer, which is the truth.

Dependency-light (only :mod:`socialhome.crypto` and the canonical JSON) so the
GFS process and the household share it, like :mod:`.gfs_proof`.
"""

from __future__ import annotations

import binascii
from dataclasses import dataclass

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

#: How old a question's ``ts`` may be when the GFS reads it (seconds). The
#: household floors its clock to the minute, so this is ~9–10 min of real age.
CLIENT_STATUS_REQUEST_MAX_AGE_S: int = 600
#: How far ahead of the GFS's clock a question's ``ts`` may be (skew).
CLIENT_STATUS_REQUEST_MAX_FUTURE_S: int = 120

_MAX_INSTANCE_ID_CHARS = 256
#: An Ed25519 signature is 64 bytes → 86 b64url chars.
_MAX_SIG_CHARS = 128
_ED25519_SIG_BYTES = 64
_MAX_SUITE_CHARS = 64


@dataclass(slots=True, frozen=True)
class StatusRequest:
    """A household's signed question, as sent in the ``client`` block."""

    instance_id: str
    sig: str
    suite: str
    #: Unix seconds, floored to the minute (:func:`status_request_ts`).
    ts: int

    def to_wire(self) -> dict[str, str | int]:
        """The ``client`` block for the ``/gfs/proof`` body."""
        return {
            "instance_id": self.instance_id,
            "sig": self.sig,
            "sig_suite": self.suite,
            "ts": self.ts,
        }


def status_request_ts(now: float) -> int:
    """*now* (unix seconds) floored to the minute — a question's ``ts``."""
    seconds = int(now)
    return seconds - seconds % 60


def status_request_signing_bytes(
    gfs_key_hex: str, instance_id: str, nonce: str, suite: str, ts: int
) -> bytes:
    """Canonical, domain-separated bytes the household signs to ask the GFS
    holding *gfs_key_hex*."""
    body = canonical_str(
        {
            "gfs_key": gfs_key_hex.strip().lower(),
            "instance_id": instance_id,
            "nonce": nonce,
            "suite": suite,
            "ts": ts,
        }
    )
    return CLIENT_STATUS_REQUEST_PREFIX + body.encode("utf-8")


def sign_status_request(
    seed: bytes, gfs_key_hex: str, instance_id: str, nonce: str, *, ts: int
) -> StatusRequest:
    """The question a household sends the GFS whose key it pinned
    (*gfs_key_hex*); ``.to_wire()`` is the ``client`` block."""
    suite = CLIENT_STATUS_SIG_SUITE_ED25519
    message = status_request_signing_bytes(gfs_key_hex, instance_id, nonce, suite, ts)
    sig = b64url_encode(sign_ed25519(seed, message))
    return StatusRequest(instance_id=instance_id, sig=sig, suite=suite, ts=ts)


def parse_status_request(value: object, *, now: float) -> StatusRequest | None:
    """The question in a well-formed, current ``client`` block, else
    ``None`` — the GFS answers that with an unsigned ``400`` before it signs
    anything.

    Refuses a wrong shape, a suite this build does not support (never a
    signed ``unknown`` for it), a ``sig`` that does not decode to exactly 64
    bytes, and a ``ts`` that is not an integer, older than
    :data:`CLIENT_STATUS_REQUEST_MAX_AGE_S` or further ahead of *now* than
    :data:`CLIENT_STATUS_REQUEST_MAX_FUTURE_S`. The signature itself is
    checked by the caller."""
    if not isinstance(value, dict):
        return None
    instance_id = value.get("instance_id")
    sig = value.get("sig")
    suite = value.get("sig_suite")
    ts = value.get("ts")
    if not (
        isinstance(instance_id, str) and isinstance(sig, str) and isinstance(suite, str)
    ):
        return None
    # ``bool`` is an ``int`` subclass; ``True`` is no time.
    if not isinstance(ts, int) or isinstance(ts, bool):
        return None
    if not instance_id or len(instance_id) > _MAX_INSTANCE_ID_CHARS:
        return None
    if not sig or len(sig) > _MAX_SIG_CHARS:
        return None
    if not suite or len(suite) > _MAX_SUITE_CHARS:
        return None
    if suite not in SUPPORTED_CLIENT_STATUS_SIG_SUITES:
        return None
    try:
        raw_sig = b64url_decode(sig)
    except binascii.Error, ValueError:
        return None
    if len(raw_sig) != _ED25519_SIG_BYTES:
        return None
    if ts < now - CLIENT_STATUS_REQUEST_MAX_AGE_S:
        return None
    if ts > now + CLIENT_STATUS_REQUEST_MAX_FUTURE_S:
        return None
    return StatusRequest(instance_id=instance_id, sig=sig, suite=suite, ts=ts)


def verify_status_request(
    public_key_hex: str, gfs_key_hex: str, nonce: str, request: StatusRequest
) -> bool:
    """Verify a household's question against its registered key, as a
    question for the GFS holding *gfs_key_hex* (the verifier's own key).

    Raises :class:`UnsupportedClientStatusSigSuite` for an unknown suite.
    Returns ``False`` — never raises — for malformed key or signature material.
    """
    if request.suite not in SUPPORTED_CLIENT_STATUS_SIG_SUITES:
        raise UnsupportedClientStatusSigSuite(request.suite)
    return _verify(
        public_key_hex,
        status_request_signing_bytes(
            gfs_key_hex, request.instance_id, nonce, request.suite, request.ts
        ),
        request.sig,
    )


def client_status_signing_bytes(
    gfs_key_hex: str, nonce: str, request: StatusRequest, status: str, suite: str
) -> bytes:
    """Canonical, domain-separated bytes the GFS signs to answer *request* —
    every field of the question is bound."""
    body = canonical_str(
        {
            "gfs_key": gfs_key_hex.strip().lower(),
            "instance_id": request.instance_id,
            "nonce": nonce,
            "request_sig": request.sig,
            "request_suite": request.suite,
            "request_ts": request.ts,
            "status": status,
            "suite": suite,
        }
    )
    return CLIENT_STATUS_SIGNING_PREFIX + body.encode("utf-8")


def sign_client_status(
    seed: bytes, nonce: str, request: StatusRequest, status: str
) -> tuple[str, str]:
    """Sign the GFS's answer to *request* with its identity seed →
    ``(client_status_sig, client_status_sig_suite)``."""
    if status not in CLIENT_STATUSES:
        raise ValueError(f"unknown client status {status!r}")
    suite = CLIENT_STATUS_SIG_SUITE_ED25519
    gfs_key_hex = ed25519_public_key(seed).hex()
    message = client_status_signing_bytes(gfs_key_hex, nonce, request, status, suite)
    return b64url_encode(sign_ed25519(seed, message)), suite


def verify_client_status(
    gfs_key_hex: str,
    nonce: str,
    request: StatusRequest,
    status: str,
    sig_b64url: str,
    suite: str,
) -> bool:
    """Verify a GFS's answer against the key the household PINNED, as the
    answer to its own question *request* (exactly as the household sent it).

    Raises :class:`UnsupportedClientStatusSigSuite` for an unknown suite.
    Returns ``False`` — never raises — for a status outside
    :data:`CLIENT_STATUSES`, malformed material or a failed verification:
    every input here is remote-controlled.
    """
    if suite not in SUPPORTED_CLIENT_STATUS_SIG_SUITES:
        raise UnsupportedClientStatusSigSuite(suite)
    if status not in CLIENT_STATUSES:
        return False
    message = client_status_signing_bytes(gfs_key_hex, nonce, request, status, suite)
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
