"""The signed envelope + sealed chunk of one binary media frame.

Both binary media transports carry the same two parts:

* the ``fed-media-v1`` DataChannel (proto v_14), one frame per message;
* the HTTPS inbox (proto v_81): one frame per request body, POSTed to the
  peer's own inbox URL behind :data:`~socialhome.federation.media_framing.MEDIA_BODY_MAGIC`
  and a suite byte (:func:`encode_media_body`).

The **header** is an ordinary signed federation envelope — the exact field
set and canonical order of :meth:`FederationService.send_event`, so the
receiver's §24.11 pipeline verifies it unchanged. Only routing fields are
plaintext (``event_type``, ``from_instance``, ``to_instance``,
``space_id``, …); everything else — the correlation ids, filename, mime,
sizes, chunk sequencing — is in ``encrypted_payload``, together with:

* ``chunk_sha256`` — b64url SHA-256 of the plaintext chunk. It is inside
  the signed bytes (``encrypted_payload`` is signed), so it binds the
  binary payload to the signature: swap the body and the hash no longer
  matches; change the hash and the signature (and GCM tag) fail.
* ``media_aead_suite`` — how the chunk is sealed
  (:data:`~socialhome.federation.media_framing.MEDIA_AEAD_SUITE_AESGCM_256`).
* ``media_frame_suite`` — the frame format
  (:data:`~socialhome.federation.media_framing.MEDIA_FRAME_SUITE_V1`).

The **payload** is the chunk sealed with AES-256-GCM under the directional
session key as raw ``nonce || ciphertext+tag`` — never base64.

Pure module — no I/O.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import orjson

from ..crypto import b64url_encode
from ..domain.federation import FederationEventType
from . import media_framing

if TYPE_CHECKING:
    from .encoder import FederationEncoder

#: ``Content-Type`` of a binary media POST. Informational only: the
#: receiver decides from the body (a relay may rewrite the header).
MEDIA_HTTPS_CONTENT_TYPE: str = "application/octet-stream"

#: The only events a binary media body may carry — a signed envelope of any
#: other type belongs in a JSON body and is refused before any crypto
#: (:func:`~socialhome.federation.inbound_validator.make_check_event_types`).
MEDIA_HTTPS_EVENT_TYPES: frozenset[FederationEventType] = frozenset(
    {FederationEventType.SPACE_MEDIA_BLOB, FederationEventType.DM_MEDIA_BLOB}
)

#: What a binary frame on the GFS relay (v_87) may carry: a media chunk,
#: or a pending moderation item whose content does not fit a JSON relay
#: envelope — the same signed frame, the item's content as its bytes
#: (``services/space_moderation_federation.py``). The relay is the one path
#: with the small JSON cap, so only its pipeline takes the wider set.
RELAY_FRAME_EVENT_TYPES: frozenset[FederationEventType] = MEDIA_HTTPS_EVENT_TYPES | {
    FederationEventType.SPACE_MODERATION_SUBMITTED
}


def build_media_envelope(
    *,
    encoder: FederationEncoder,
    session_key: bytes,
    from_instance: str,
    to_instance: str,
    sig_suite: str,
    event_type: FederationEventType,
    payload: dict,
    raw_chunk: bytes,
    space_id: str | None,
) -> tuple[dict, bytes]:
    """Return ``(signed_header, sealed_chunk)`` for one media chunk.

    ``payload`` is the chunk metadata **without** the bytes. The header's
    signed bytes are ``orjson.dumps`` of the envelope without
    ``signatures``, in the canonical order the inbound
    ``verify_signature`` step reconstructs.
    """
    metadata = {
        **payload,
        "chunk_sha256": b64url_encode(hashlib.sha256(raw_chunk).digest()),
        "media_aead_suite": media_framing.MEDIA_AEAD_SUITE_AESGCM_256,
        "media_frame_suite": media_framing.MEDIA_FRAME_SUITE_V1,
    }
    envelope: dict = {
        "msg_id": str(uuid.uuid4()),
        "event_type": event_type.value,
        "from_instance": from_instance,
        "to_instance": to_instance,
        "timestamp": datetime.now(UTC).isoformat(),
        "encrypted_payload": encoder.encrypt_payload(
            orjson.dumps(metadata).decode("utf-8"), session_key
        ),
        "space_id": space_id,
        "proto_version": 1,
        "sig_suite": sig_suite,
    }
    envelope["signatures"] = encoder.sign_envelope_all(
        orjson.dumps(envelope), suite=sig_suite
    )
    return envelope, encoder.encrypt_bytes(raw_chunk, session_key)


def encode_media_body(header: dict, sealed_chunk: bytes) -> bytes:
    """The HTTPS inbox body for one chunk: magic, suite byte, then one
    ``fed-media-v1`` frame — the signed header JSON verbatim + the sealed
    chunk. (The DataChannel sends the bare frame.)"""
    return media_framing.encode_body(orjson.dumps(header), sealed_chunk)
