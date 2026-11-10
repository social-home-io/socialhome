"""Multi-recipient mailbox frames (v_90): one sealed body, many mailboxes.

A space event whose bytes are identical for every member household that
reaches the space through the same GFS is uploaded ONCE
(``POST /gfs/envelope/multi`` with a list of mailbox ids) instead of once per
mailbox. The GFS pushes the same frame ``{"type": "envelope", "sealed":
{mr_suite, kid, ciphertext}}`` to each listed mailbox.

Keys
----
All three come from the space content key of one ``(space, epoch)`` — the
content key itself never leaves
:class:`~socialhome.services.space_crypto_service.SpaceContentEncryption`
(same rule as its ``media_key``):

* ``kid`` — 16 HKDF-SHA256 bytes (info ``sh-mr-kid:v1``), b64url. Tells a
  receiver which of its space keys opens the frame; reveals nothing else.
* ``key`` — 32 HKDF-SHA256 bytes (info ``sh-mr-key:v1``), the AES-256-GCM
  key for both the outer seal and the inner ``encrypted_payload``.
* ``tag_key`` — 32 HKDF-SHA256 bytes (info ``sh-mr-tag:v1``), the HMAC key
  of the audience tags only — the AES-GCM key is never also an HMAC key.

All three travel together as one :class:`MrKeys`.

Every member holds the content key, so every member can open the frame; a
non-member (the GFS, a relay) cannot. The sender is proved by the inner
envelope's Ed25519 signature under its identity key, exactly as for a
per-recipient envelope.

Wire
----
The inner envelope is the ordinary §24.11 shape with ``to_instance =
"*:" + ".".join(tags)`` — :data:`MULTI_TO_INSTANCE` as the marker, then one
:func:`recipient_tag` per intended recipient household, sorted — signed in
the canonical field order of
``FederationService._seal_envelope`` / ``make_verify_signature``. The outer
seal's plaintext is :func:`~socialhome.federation.gfs_relay_transport
.build_relay_plaintext` (the relay ``kind`` wrapper padded to one of
``RELAY_SIZE_BUCKETS``), sealed under ``key`` with AAD
``b"sh-mr-seal:v1\\n" + mr_suite + "\\n" + kid``.

Audience
--------
The signed tag list binds the frame to the households the sender meant:
a receiver takes it only when its own tag is listed
(:func:`multi_audience_includes`). Without it, anyone holding the epoch key
— the GFS cannot, but a member or a follower can — could re-push a captured
frame to a mailbox the sender left out (a follower, a household on the
per-pair hide list). A tag is ``b64url(HMAC-SHA256(tag_key, "sh-mr-to:v1\n" +
instance_id)[:4])``: it sits inside the seal, so the GFS never sees it, and
only a key holder who already knows an instance id can test for it.

The GFS sees the mailbox list, the time, the size bucket and the sender's
IP — never the space, an instance id, the event type or the content.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import os
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeGuard

import orjson
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..crypto import b64url_decode, b64url_encode
from ..domain.errors import WrongTypeError
from ..domain.gfs_mailbox import MULTI_MAX_RECIPIENTS
from .encoder import FederationEncoder
from .gfs_relay_transport import RELAY_SIZE_BUCKETS, build_relay_plaintext

#: The one multi-recipient seal suite: AES-256-GCM under the HKDF-derived
#: multi-recipient key, padded relay plaintext.
MR_SUITE_AESGCM_256: str = "mr-aesgcm-256"
SUPPORTED_MR_SUITES: frozenset[str] = frozenset({MR_SUITE_AESGCM_256})

#: The marker every multi-recipient ``to_instance`` starts with — followed
#: by ``":"`` and the recipient tag list. No ordinary envelope's
#: ``to_instance`` (an instance id) ever starts with it.
MULTI_TO_INSTANCE: str = "*"

#: The signature suite of a multi-recipient envelope. One body serves every
#: recipient, so it cannot follow a per-peer negotiated suite.
MR_SIG_SUITE: str = "ed25519"

_HKDF_SALT = b"sh-mr-v1"
_KID_INFO = b"sh-mr-kid:v1"
_KEY_INFO = b"sh-mr-key:v1"
_TAG_KEY_INFO = b"sh-mr-tag:v1"
_AAD_PREFIX = b"sh-mr-seal:v1\n"
_TAG_PREFIX = b"sh-mr-to:v1\n"
_TAG_BYTES_TRUNC = 4
_AUDIENCE_PREFIX = MULTI_TO_INSTANCE + ":"
#: A 4-byte tag in unpadded b64url (4 bytes keep a 200-recipient frame in
#: the 4 KiB bucket; a non-listed household matches a listed tag with
#: probability N / 2**32, ~5e-8 at N = 200).
_TAG_RE = re.compile(r"[A-Za-z0-9_-]{6}")
#: A 16-byte kid in unpadded b64url.
_KID_RE = re.compile(r"[A-Za-z0-9_-]{22}")
_KID_BYTES = 16
_KEY_BYTES = 32
_NONCE_BYTES = 12
_TAG_BYTES = 16

#: Longest ``ciphertext`` string a receiver decodes: the top bucket plus
#: nonce and tag, in unpadded b64url. Anything longer is refused before
#: any decode.
MAX_MR_CIPHERTEXT_CHARS: int = -(
    -(RELAY_SIZE_BUCKETS[-1] + _NONCE_BYTES + _TAG_BYTES) * 4 // 3
)


class UnsupportedMrSuite(ValueError):
    """A multi-recipient seal names a suite this build does not support."""


def _hkdf(content_key: bytes, *, length: int, info: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=length, salt=_HKDF_SALT, info=info
    ).derive(content_key)


@dataclass(slots=True, frozen=True)
class MrKeys:
    """The multi-recipient material of one ``(space, epoch)``: its ``kid``,
    the AES-256-GCM ``key`` and the audience-tag HMAC ``tag_key``. The repr
    names the kid only. Never log either key."""

    kid: str
    key: bytes = field(repr=False)
    tag_key: bytes = field(repr=False)


def derive_mr_key(content_key: bytes) -> MrKeys:
    """The :class:`MrKeys` of one epoch's content key (HKDF-SHA256, one
    ``info`` label each). Never log the keys."""
    if len(content_key) != 32:
        raise ValueError("space content key must be 32 bytes")
    return MrKeys(
        kid=b64url_encode(_hkdf(content_key, length=_KID_BYTES, info=_KID_INFO)),
        key=_hkdf(content_key, length=_KEY_BYTES, info=_KEY_INFO),
        tag_key=_hkdf(content_key, length=_KEY_BYTES, info=_TAG_KEY_INFO),
    )


def is_valid_kid(kid: object) -> TypeGuard[str]:
    """True when *kid* has the one shape :func:`derive_mr_key` makes (16
    bytes in unpadded b64url, 22 chars). Checked before any key lookup."""
    return isinstance(kid, str) and _KID_RE.fullmatch(kid) is not None


def recipient_tag(tag_key: bytes, instance_id: str) -> str:
    """The audience tag of one recipient household under the
    multi-recipient *tag_key* (:attr:`MrKeys.tag_key`, 6 chars). Never log
    it."""
    mac = hmac.new(tag_key, _TAG_PREFIX + instance_id.encode("utf-8"), hashlib.sha256)
    return b64url_encode(mac.digest()[:_TAG_BYTES_TRUNC])


def multi_to_instance(tag_key: bytes, recipients: Collection[str]) -> str:
    """The signed ``to_instance`` of a multi frame for *recipients*:
    ``"*:" + ".".join(sorted tags)``. :class:`ValueError` for an empty
    audience or one over :data:`MULTI_MAX_RECIPIENTS`."""
    tags = sorted({recipient_tag(tag_key, r) for r in recipients})
    if not 1 <= len(tags) <= MULTI_MAX_RECIPIENTS:
        raise ValueError(
            f"a multi-recipient frame names 1..{MULTI_MAX_RECIPIENTS} "
            f"recipient households, not {len(tags)}"
        )
    return _AUDIENCE_PREFIX + ".".join(tags)


def multi_audience_includes(
    to_instance: object, tag_key: bytes, own_instance_id: str
) -> bool:
    """True when *to_instance* is a well-formed audience (1..
    :data:`MULTI_MAX_RECIPIENTS` valid tags, sorted, no repeats) that lists
    *own_instance_id*'s tag under *tag_key*. False for anything else."""
    if not isinstance(to_instance, str) or not to_instance.startswith(_AUDIENCE_PREFIX):
        return False
    tags = to_instance[len(_AUDIENCE_PREFIX) :].split(".")
    if not 1 <= len(tags) <= MULTI_MAX_RECIPIENTS:
        return False
    if any(_TAG_RE.fullmatch(t) is None for t in tags):
        return False
    if any(a >= b for a, b in itertools.pairwise(tags)):
        return False  # unsorted or repeated: not the shape a sender makes
    own = recipient_tag(tag_key, own_instance_id)
    return any(hmac.compare_digest(own, t) for t in tags)


def _aad(suite: str, kid: str) -> bytes:
    return _AAD_PREFIX + suite.encode("utf-8") + b"\n" + kid.encode("utf-8")


def encrypt_mr_payload(payload_json: str, key: bytes) -> str:
    """AES-256-GCM → ``b64url(nonce):b64url(ct)`` (the encoder's shape)."""
    nonce = os.urandom(_NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, payload_json.encode("utf-8"), None)
    return b64url_encode(nonce) + ":" + b64url_encode(ct)


def decrypt_mr_payload(enc: str, key: bytes) -> str:
    """Inverse of :func:`encrypt_mr_payload`. :class:`ValueError` on any
    failure (malformed, wrong key, tampered)."""
    if not isinstance(enc, str) or ":" not in enc:
        raise ValueError("malformed multi-recipient payload")
    nonce_b64, ct_b64 = enc.split(":", 1)
    try:
        plaintext = AESGCM(key).decrypt(
            b64url_decode(nonce_b64), b64url_decode(ct_b64), None
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
    keys: MrKeys,
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
        "to_instance": multi_to_instance(keys.tag_key, recipients),
        "timestamp": datetime.now(UTC).isoformat(),
        "encrypted_payload": encrypt_mr_payload(payload_json, keys.key),
        "space_id": space_id,
        "proto_version": 1,
        "sig_suite": MR_SIG_SUITE,
    }
    envelope["signatures"] = encoder.sign_envelope_all(
        orjson.dumps(envelope), suite=MR_SIG_SUITE
    )
    return envelope


def seal_multi(envelope_dict: dict, *, kid: str, key: bytes) -> dict[str, str]:
    """Pad and seal *envelope_dict* under the multi-recipient *key*.

    Raises :class:`ValueError` when it does not fit the top relay bucket.
    """
    plaintext = build_relay_plaintext(envelope_dict)
    if len(plaintext) > RELAY_SIZE_BUCKETS[-1]:
        raise ValueError(
            f"multi-recipient envelope is {len(plaintext)} bytes once wrapped, "
            f"over the top size bucket ({RELAY_SIZE_BUCKETS[-1]})"
        )
    nonce = os.urandom(_NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, plaintext, _aad(MR_SUITE_AESGCM_256, kid))
    return {
        "mr_suite": MR_SUITE_AESGCM_256,
        "kid": kid,
        "ciphertext": b64url_encode(nonce + ct),
    }


def is_multi_sealed(sealed: object) -> TypeGuard[dict]:
    """True when *sealed* is a multi-recipient seal (it names an
    ``mr_suite``) rather than a per-recipient KEM seal."""
    return isinstance(sealed, dict) and "mr_suite" in sealed


def require_mr_suite(sealed: dict) -> str:
    """The seal's suite when supported — :class:`UnsupportedMrSuite`
    otherwise (no default)."""
    suite = sealed.get("mr_suite")
    if not isinstance(suite, str) or suite not in SUPPORTED_MR_SUITES:
        raise UnsupportedMrSuite(f"unsupported multi-recipient suite {suite!r}")
    return suite


def open_multi(sealed: dict, key: bytes) -> dict:
    """Open a multi-recipient seal → the relay body (``{kind, envelope,
    _pad}``). The suite is validated first. :class:`ValueError` on any
    failure; the caller checks the body's ``kind``."""
    suite = require_mr_suite(sealed)
    kid = sealed.get("kid")
    ciphertext = sealed.get("ciphertext")
    if not is_valid_kid(kid):
        raise ValueError("multi-recipient seal has a malformed kid")
    if not isinstance(ciphertext, str) or not ciphertext:
        raise ValueError("multi-recipient seal missing ciphertext")
    if len(ciphertext) > MAX_MR_CIPHERTEXT_CHARS:
        raise ValueError("multi-recipient seal too large")
    try:
        raw = b64url_decode(ciphertext)
    except Exception as exc:
        raise ValueError("multi-recipient ciphertext is not b64url") from exc
    if len(raw) < _NONCE_BYTES + _TAG_BYTES:
        raise ValueError("multi-recipient ciphertext too short")
    try:
        plaintext = AESGCM(key).decrypt(
            raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], _aad(suite, kid)
        )
    except Exception as exc:  # InvalidTag
        raise ValueError("multi-recipient seal does not open") from exc
    try:
        body = orjson.loads(plaintext)
    except orjson.JSONDecodeError as exc:
        raise ValueError("multi-recipient body is not JSON") from exc
    if not isinstance(body, dict):
        raise WrongTypeError("multi-recipient body is not an object")
    return body


__all__ = [
    "MAX_MR_CIPHERTEXT_CHARS",
    "MR_SIG_SUITE",
    "MR_SUITE_AESGCM_256",
    "MULTI_TO_INSTANCE",
    "SUPPORTED_MR_SUITES",
    "MrKeys",
    "UnsupportedMrSuite",
    "build_multi_envelope",
    "decrypt_mr_payload",
    "derive_mr_key",
    "encrypt_mr_payload",
    "is_multi_sealed",
    "is_valid_kid",
    "multi_audience_includes",
    "multi_to_instance",
    "open_multi",
    "recipient_tag",
    "require_mr_suite",
    "seal_multi",
]
