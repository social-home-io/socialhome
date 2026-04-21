"""Sealed sender — encrypt the sender identity for GFS-relayed events (§4.3).

When a public-space event flows through the Global Federation Server
(GFS), the GFS needs to know:

* the **space_id** to route to subscribers,
* the **epoch** to pick the right key.

It does NOT need to know **which instance** sent it.  Sealed sender
hides ``from_instance`` from the GFS by encrypting it under the space's
content key (the same key used by :class:`SpaceContentEncryption`).
The recipient decrypts the inner envelope, extracts the original
``from_instance`` and the signature, and verifies as usual.

Wire format::

    {
      "sealed":            true,
      "space_id":          "sp-xyz",          # plaintext (routing)
      "epoch":             3,                 # plaintext (key selection)
      "encrypted_sender":  "<nonce>:<ct>",    # AES-256-GCM
      "encrypted_payload": "<nonce>:<ct>",    # space payload encryption
      "outer_signature":   "<sig>"            # GFS-visible signature
                                              # (over space_id + epoch + ciphertexts)
    }

The recipient runs:

    sender = decrypt(encrypted_sender, space_key)
    payload = decrypt(encrypted_payload, space_key)
    verify(outer_signature, sender_pk_lookup(sender))

A GFS that drops or substitutes the outer_signature can be detected by
the recipient (signature mismatch). A GFS cannot read the sender field.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..crypto import b64url_decode, b64url_encode


_NONCE_BYTES = 12


@dataclass(slots=True, frozen=True)
class SealedEnvelope:
    """The structure produced by :func:`seal_envelope`."""

    space_id: str
    epoch: int
    encrypted_sender: str
    encrypted_payload: str

    def to_dict(self) -> dict:
        return {
            "sealed": True,
            "space_id": self.space_id,
            "epoch": self.epoch,
            "encrypted_sender": self.encrypted_sender,
            "encrypted_payload": self.encrypted_payload,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SealedEnvelope":
        if not data.get("sealed"):
            raise ValueError("Envelope is not sealed")
        try:
            return cls(
                space_id=str(data["space_id"]),
                epoch=int(data["epoch"]),
                encrypted_sender=str(data["encrypted_sender"]),
                encrypted_payload=str(data["encrypted_payload"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Malformed sealed envelope: {exc}") from exc


def seal_envelope(
    *,
    space_id: str,
    epoch: int,
    sender_instance_id: str,
    payload_json: str,
    space_content_key: bytes,
) -> SealedEnvelope:
    """Encrypt sender + payload under the per-epoch space key.

    ``space_content_key`` is the raw 32-byte AES key already unwrapped
    by :class:`SpaceContentEncryption`.  Callers should not pass the
    KEK-wrapped form — the wire-level KEK is for at-rest only.
    """
    if len(space_content_key) != 32:
        raise ValueError("space_content_key must be 32 bytes")
    aead = AESGCM(space_content_key)
    aad = f"{space_id}:{epoch}".encode("utf-8")

    sender_nonce = os.urandom(_NONCE_BYTES)
    payload_nonce = os.urandom(_NONCE_BYTES)

    sender_ct = aead.encrypt(
        sender_nonce,
        sender_instance_id.encode("utf-8"),
        aad,
    )
    payload_ct = aead.encrypt(
        payload_nonce,
        payload_json.encode("utf-8"),
        aad,
    )
    return SealedEnvelope(
        space_id=space_id,
        epoch=epoch,
        encrypted_sender=_pack(sender_nonce, sender_ct),
        encrypted_payload=_pack(payload_nonce, payload_ct),
    )


@dataclass(slots=True, frozen=True)
class UnsealedContent:
    sender_instance_id: str
    payload: dict


def unseal_envelope(
    envelope: SealedEnvelope,
    *,
    space_content_key: bytes,
) -> UnsealedContent:
    """Inverse of :func:`seal_envelope`."""
    if len(space_content_key) != 32:
        raise ValueError("space_content_key must be 32 bytes")
    aead = AESGCM(space_content_key)
    aad = f"{envelope.space_id}:{envelope.epoch}".encode("utf-8")

    sender_nonce, sender_ct = _unpack(envelope.encrypted_sender)
    payload_nonce, payload_ct = _unpack(envelope.encrypted_payload)

    sender = aead.decrypt(sender_nonce, sender_ct, aad).decode("utf-8")
    payload = aead.decrypt(payload_nonce, payload_ct, aad).decode("utf-8")
    return UnsealedContent(
        sender_instance_id=sender,
        payload=json.loads(payload),
    )


# ─── Internal pack / unpack ──────────────────────────────────────────────


def _pack(nonce: bytes, ct: bytes) -> str:
    return b64url_encode(nonce) + ":" + b64url_encode(ct)


def _unpack(wire: str) -> tuple[bytes, bytes]:
    try:
        nonce_b64, ct_b64 = wire.split(":", 1)
    except ValueError as exc:
        raise ValueError(f"Malformed sealed wire format: {wire!r}") from exc
    return b64url_decode(nonce_b64), b64url_decode(ct_b64)
