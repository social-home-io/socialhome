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
from ..domain.federation import (
    SPACE_SESSION_ALLOWED_EVENT_TYPES,
    FederationEventType,
)
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

#: What a binary frame on the GFS relay may carry: a media chunk (v_87),
#: or — v_95 — any event a household behind a GFS may send or receive at
#: all (:data:`~socialhome.domain.federation.SPACE_SESSION_ALLOWED_EVENT_TYPES`)
#: whose sealed envelope does not fit a JSON envelope on that relay. The
#: frame is the same signed frame; the event's payload JSON is its bytes
#: (:data:`FRAME_BINDING_FIELDS` is then all its signed metadata holds). The
#: relay is the one path with the small JSON cap, so only its pipeline takes
#: the wider set — and every gate of a JSON envelope still runs on it.
RELAY_FRAME_EVENT_TYPES: frozenset[FederationEventType] = (
    MEDIA_HTTPS_EVENT_TYPES | SPACE_SESSION_ALLOWED_EVENT_TYPES
)

#: The smallest payload a framed event may carry. A sender frames only
#: what no JSON relay envelope takes (a payload over ~140 KiB: the 191 KiB
#: envelope holds it encrypted and base64'ed); a receiver refuses a framed
#: event well below that, so the frame is never a second lane — with the
#: media leg's own budgets — for ordinary events.
RELAY_EVENT_FRAME_MIN_PAYLOAD_BYTES: int = 96 * 1024

#: The signed metadata fields that bind a frame's bytes to its envelope and
#: name how they are sealed. A framed event (v_95) carries these and nothing
#: else there: its payload is the object in the frame's bytes.
FRAME_BINDING_FIELDS: frozenset[str] = frozenset(
    {"chunk_sha256", "media_aead_suite", "media_frame_suite"}
)


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
    msg_id: str | None = None,
) -> tuple[dict, bytes]:
    """Return ``(signed_header, sealed_chunk)`` for one media chunk.

    ``payload`` is the chunk metadata **without** the bytes. ``msg_id``
    defaults to a fresh one; a framed event (v_95) passes its envelope's,
    so a retry from the outbox is the same message to the receiver's replay
    cache. The header's
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
        "msg_id": msg_id or str(uuid.uuid4()),
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
