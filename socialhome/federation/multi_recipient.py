"""Multi-recipient mailbox frames (v_90): one sealed body, many mailboxes.

A space event whose bytes are identical for every member household that
reaches the space through the same GFS is uploaded ONCE
(``POST /gfs/envelope/multi``) instead of once per mailbox:

.. code-block:: text

    {"to":   [{"mailbox": "<id>", "wrap": {kem_suite, eph_pk, ciphertext}}, ...],
     "body": {"mr_suite": "mr-wrap-aesgcm-256", "ciphertext": "<b64url>"}}

The GFS stores ``body`` once and each ``wrap`` with its mailbox's reference,
and pushes ``{"type": "envelope", "sealed": {mr_suite, ciphertext, wrap}}``
to each listed mailbox.

Keys
----
A fresh random 32-byte key *K* per upload attempt seals the body (AES-256-GCM,
AAD ``b"sh-mr-body:v1\\n" + mr_suite``) and the inner envelope's
``encrypted_payload`` (AAD ``b"sh-mr-payload:v1"``). *K* is wrapped to each
recipient mailbox's ``kem_pk`` with the existing static-recipient seal
(:func:`~socialhome.federation.keywrap_seal.seal_to_keywrap` — the same
``{kem_suite, eph_pk, ciphertext}`` dict a single mailbox frame's ``sealed``
is). Only a listed mailbox's key opens its wrap; the GFS, a relay or any
other key holder cannot. No space key or epoch is involved.

Wire
----
The inner envelope is the ordinary §24.11 shape with ``to_instance =
"*:" + ".".join(tags)`` — :data:`MULTI_TO_INSTANCE` as the marker, then one
:func:`recipient_tag` per intended recipient household, sorted — signed with
``ed25519`` by the sender's identity key in the canonical field order of
``FederationService._seal_envelope`` / ``make_verify_signature``. The body's
plaintext is :func:`~socialhome.federation.gfs_relay_transport
.build_relay_plaintext` (the relay ``kind`` wrapper padded to one of
``RELAY_SIZE_BUCKETS``).

Audience
--------
Every recipient learns *K*, so the signed tag list binds the frame to the
households the sender meant: a receiver takes it only when its own tag is
listed (:func:`multi_audience_includes`) — a recipient cannot re-push a
captured frame (with a fresh wrap of *K*) to a household the sender left
out. A tag is ``b64url(SHA-256("sh-mr-to:v1\\n" + instance_id)[:4])``,
unkeyed: the list sits inside the body, so the GFS never sees it.

The GFS sees the mailbox list, one opaque wrap per mailbox, the time, the
size bucket and the sender's IP — never the space, an instance id, the
event type or the content.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import re
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any, TypeGuard

import orjson
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..crypto import b64url_decode, b64url_encode
from ..domain.errors import WrongTypeError
from ..domain.gfs_mailbox import MULTI_MAX_RECIPIENTS
from .encoder import FederationEncoder
from .gfs_relay_transport import RELAY_SIZE_BUCKETS, build_relay_plaintext
from .keywrap_seal import open_keywrap, seal_to_keywrap

#: The one multi-recipient suite: a random AES-256-GCM body key, wrapped per
#: recipient with the key-wrap seal (whose own ``kem_suite`` is checked too).
MR_SUITE_WRAP_AESGCM_256: str = "mr-wrap-aesgcm-256"
SUPPORTED_MR_SUITES: frozenset[str] = frozenset({MR_SUITE_WRAP_AESGCM_256})

#: The marker every multi-recipient ``to_instance`` starts with — followed
#: by ``":"`` and the recipient tag list. No ordinary envelope's
#: ``to_instance`` (an instance id) ever starts with it.
MULTI_TO_INSTANCE: str = "*"

#: The signature suite of a multi-recipient envelope. One body serves every
#: recipient, so it cannot follow a per-peer negotiated suite.
MR_SIG_SUITE: str = "ed25519"

_BODY_AAD_PREFIX = b"sh-mr-body:v1\n"
_PAYLOAD_AAD = b"sh-mr-payload:v1"
_TAG_PREFIX = b"sh-mr-to:v1\n"
_TAG_BYTES_TRUNC = 4
_AUDIENCE_PREFIX = MULTI_TO_INSTANCE + ":"
#: A 4-byte tag in unpadded b64url (4 bytes keep a 200-recipient frame in
#: the 4 KiB bucket; a non-listed household matches a listed tag with
#: probability N / 2**32, ~5e-8 at N = 200).
_TAG_RE = re.compile(r"[A-Za-z0-9_-]{6}")
_KEY_BYTES = 32
_NONCE_BYTES = 12
_GCM_TAG_BYTES = 16

#: Longest body ``ciphertext`` string a receiver decodes: the top bucket
#: plus nonce and tag, in unpadded b64url. Anything longer is refused
#: before any decode.
MAX_MR_CIPHERTEXT_CHARS: int = -(
    -(RELAY_SIZE_BUCKETS[-1] + _NONCE_BYTES + _GCM_TAG_BYTES) * 4 // 3
)


class UnsupportedMrSuite(ValueError):
    """A multi-recipient frame names a suite this build does not support."""


def new_mr_key() -> bytes:
    """A fresh random body key *K* — one per upload attempt. Never log it."""
    return os.urandom(_KEY_BYTES)


def wrap_mr_key(key: bytes, kem_pk: bytes) -> dict[str, str]:
    """*key* sealed to one recipient mailbox's key-wrap public key."""
    return seal_to_keywrap(recipient_keywrap_pub=kem_pk, plaintext=key)


def recipient_tag(instance_id: str) -> str:
    """The audience tag of one recipient household (6 chars)."""
    digest = hashlib.sha256(_TAG_PREFIX + instance_id.encode("utf-8")).digest()
    return b64url_encode(digest[:_TAG_BYTES_TRUNC])


def multi_to_instance(recipients: Collection[str]) -> str:
    """The signed ``to_instance`` of a multi frame for *recipients*:
    ``"*:" + ".".join(sorted tags)``. :class:`ValueError` for an empty
    audience or one over :data:`MULTI_MAX_RECIPIENTS`."""
    tags = sorted({recipient_tag(r) for r in recipients})
    if not 1 <= len(tags) <= MULTI_MAX_RECIPIENTS:
        raise ValueError(
            f"a multi-recipient frame names 1..{MULTI_MAX_RECIPIENTS} "
            f"recipient households, not {len(tags)}"
        )
    return _AUDIENCE_PREFIX + ".".join(tags)


def multi_audience_includes(to_instance: object, own_instance_id: str) -> bool:
    """True when *to_instance* is a well-formed audience (1..
    :data:`MULTI_MAX_RECIPIENTS` valid tags, sorted, no repeats) that lists
    *own_instance_id*'s tag. False for anything else."""
    if not isinstance(to_instance, str) or not to_instance.startswith(_AUDIENCE_PREFIX):
        return False
    tags = to_instance[len(_AUDIENCE_PREFIX) :].split(".")
    if not 1 <= len(tags) <= MULTI_MAX_RECIPIENTS:
        return False
    if any(_TAG_RE.fullmatch(t) is None for t in tags):
        return False
    if any(a >= b for a, b in itertools.pairwise(tags)):
        return False  # unsorted or repeated: not the shape a sender makes
    return recipient_tag(own_instance_id) in tags


def encrypt_mr_payload(payload_json: str, key: bytes) -> str:
    """AES-256-GCM → ``b64url(nonce):b64url(ct)`` (the encoder's shape)."""
    nonce = os.urandom(_NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, payload_json.encode("utf-8"), _PAYLOAD_AAD)
    return b64url_encode(nonce) + ":" + b64url_encode(ct)


def decrypt_mr_payload(enc: str, key: bytes) -> str:
    """Inverse of :func:`encrypt_mr_payload`. :class:`ValueError` on any
    failure (malformed, wrong key, tampered)."""
    if not isinstance(enc, str) or ":" not in enc:
        raise ValueError("malformed multi-recipient payload")
    nonce_b64, ct_b64 = enc.split(":", 1)
    try:
        plaintext = AESGCM(key).decrypt(
            b64url_decode(nonce_b64), b64url_decode(ct_b64), _PAYLOAD_AAD
        )
    except Exception as exc:  # InvalidTag, binascii.Error, ValueError
        raise ValueError("multi-recipient payload does not decrypt") from exc
    return plaintext.decode("utf-8")


def build_multi_envelope(
    *,
    encoder: FederationEncoder,
    own_instance_id: str,
    event_type: str,
    payload_json: str,
    space_id: str,
    key: bytes,
    recipients: Collection[str],
    msg_id: str,
) -> dict:
    """The signed §24.11 envelope every recipient of one multi frame gets.

    *recipients* are the instance ids of the households the frame is for
    (1..:data:`MULTI_MAX_RECIPIENTS`); their tags become the signed
    ``to_instance`` (:func:`multi_to_instance`). *msg_id* is the stored
    multi payload's, the same on every retry and per-row fallback, so the
    receivers' replay cache takes one copy.

    CANONICAL FIELD ORDER — byte-identical to ``_seal_envelope`` and
    ``make_verify_signature``; the signature covers everything but
    ``signatures``.
    """
    envelope: dict[str, Any] = {
        "msg_id": msg_id,
        "event_type": event_type,
        "from_instance": own_instance_id,
        "to_instance": multi_to_instance(recipients),
        "timestamp": datetime.now(UTC).isoformat(),
        "encrypted_payload": encrypt_mr_payload(payload_json, key),
        "space_id": space_id,
        "proto_version": 1,
        "sig_suite": MR_SIG_SUITE,
    }
    envelope["signatures"] = encoder.sign_envelope_all(
        orjson.dumps(envelope), suite=MR_SIG_SUITE
    )
    return envelope


def _body_aad(suite: str) -> bytes:
    return _BODY_AAD_PREFIX + suite.encode("utf-8")


def seal_multi(envelope_dict: dict, *, key: bytes) -> dict[str, str]:
    """Pad and seal *envelope_dict* under the body key → the upload's
    ``body`` (``{mr_suite, ciphertext}``).

    Raises :class:`ValueError` when it does not fit the top relay bucket.
    """
    plaintext = build_relay_plaintext(envelope_dict)
    if len(plaintext) > RELAY_SIZE_BUCKETS[-1]:
        raise ValueError(
            f"multi-recipient envelope is {len(plaintext)} bytes once wrapped, "
            f"over the top size bucket ({RELAY_SIZE_BUCKETS[-1]})"
        )
    nonce = os.urandom(_NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, plaintext, _body_aad(MR_SUITE_WRAP_AESGCM_256))
    return {
        "mr_suite": MR_SUITE_WRAP_AESGCM_256,
        "ciphertext": b64url_encode(nonce + ct),
    }


def is_multi_sealed(sealed: object) -> TypeGuard[dict]:
    """True when *sealed* is a multi-recipient frame (it names an
    ``mr_suite``) rather than a per-recipient key-wrap seal."""
    return isinstance(sealed, dict) and "mr_suite" in sealed


def require_mr_suite(sealed: dict) -> str:
    """The frame's suite when supported — :class:`UnsupportedMrSuite`
    otherwise (no default)."""
    suite = sealed.get("mr_suite")
    if not isinstance(suite, str) or suite not in SUPPORTED_MR_SUITES:
        raise UnsupportedMrSuite(f"unsupported multi-recipient suite {suite!r}")
    return suite


def open_multi(sealed: dict, kem_private: bytes) -> tuple[dict, bytes]:
    """Open a multi-recipient frame ``{mr_suite, ciphertext, wrap}`` with
    our mailbox's key-wrap private key → ``(relay body, K)``.

    The suite is judged first, then the wrap (its own ``kem_suite`` check,
    :class:`~socialhome.federation.keywrap_seal.UnsupportedKemSuite`) opens
    to a 32-byte *K*, which opens the body. :class:`ValueError` on any
    failure; the caller checks the body's ``kind``. Never log *K*."""
    suite = require_mr_suite(sealed)
    wrap = sealed.get("wrap")
    ciphertext = sealed.get("ciphertext")
    if not isinstance(wrap, dict):
        raise WrongTypeError("multi-recipient frame carries no wrap")
    if not isinstance(ciphertext, str) or not ciphertext:
        raise ValueError("multi-recipient frame missing ciphertext")
    if len(ciphertext) > MAX_MR_CIPHERTEXT_CHARS:
        raise ValueError("multi-recipient frame too large")
    try:
        key = open_keywrap(sealed=wrap, recipient_keywrap_priv=kem_private)
    except InvalidTag as exc:  # UnsupportedKemSuite / malformed: ValueError
        raise ValueError("multi-recipient wrap does not open") from exc
    if len(key) != _KEY_BYTES:
        raise ValueError("multi-recipient wrap holds no 32-byte key")
    try:
        raw = b64url_decode(ciphertext)
    except Exception as exc:
        raise ValueError("multi-recipient ciphertext is not b64url") from exc
    if len(raw) < _NONCE_BYTES + _GCM_TAG_BYTES:
        raise ValueError("multi-recipient ciphertext too short")
    try:
        plaintext = AESGCM(key).decrypt(
            raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], _body_aad(suite)
        )
    except Exception as exc:  # InvalidTag
        raise ValueError("multi-recipient body does not open") from exc
    try:
        body = orjson.loads(plaintext)
    except orjson.JSONDecodeError as exc:
        raise ValueError("multi-recipient body is not JSON") from exc
    if not isinstance(body, dict):
        raise WrongTypeError("multi-recipient body is not an object")
    return body, key


__all__ = [
    "MAX_MR_CIPHERTEXT_CHARS",
    "MR_SIG_SUITE",
    "MR_SUITE_WRAP_AESGCM_256",
    "MULTI_TO_INSTANCE",
    "SUPPORTED_MR_SUITES",
    "UnsupportedMrSuite",
    "build_multi_envelope",
    "decrypt_mr_payload",
    "encrypt_mr_payload",
    "is_multi_sealed",
    "multi_audience_includes",
    "multi_to_instance",
    "new_mr_key",
    "open_multi",
    "recipient_tag",
    "require_mr_suite",
    "seal_multi",
    "wrap_mr_key",
]
