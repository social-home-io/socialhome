"""§24.11 + §25.8.21 guards for binary media over HTTPS (proto v_81).

Release-blocker protocol tests (marked ``security``) for
``POST /federation/inbox/{inbox_id}/media``:

* the frame runs the SAME §24.11 chain as the JSON inbox — every step, in
  the same order, plus one event-type gate (never a forked copy);
* encryption-first: only routing fields are cleartext; the chunk, the
  correlation ids, filename, mime and sizes are sealed;
* the signature covers the binary body through ``chunk_sha256`` inside the
  signed, encrypted metadata;
* the frame suite is a closed set with a dedicated rejection, and the body
  cap is one chunk plus a small overhead.
"""

from __future__ import annotations

import hashlib
import os
from unittest.mock import MagicMock

import orjson
import pytest

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.domain.federation import FederationEventType
from socialhome.domain.media_chunking import HTTPS_CHUNK_BYTES
from socialhome.federation import FederationService
from socialhome.federation import media_framing as mf
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.media_envelope import (
    MEDIA_HTTPS_EVENT_TYPES,
    build_media_envelope,
    encode_media_body,
)
from socialhome.infrastructure.event_bus import EventBus

pytestmark = pytest.mark.security

#: The envelope fields a router needs — nothing else may be cleartext.
_ROUTING_FIELDS = {
    "msg_id",
    "event_type",
    "from_instance",
    "to_instance",
    "timestamp",
    "encrypted_payload",
    "space_id",
    "proto_version",
    "sig_suite",
    "signatures",
}


def _body(raw: bytes, session: bytes, payload: dict) -> tuple[bytes, dict]:
    kp = generate_identity_keypair()
    header, sealed = build_media_envelope(
        encoder=FederationEncoder(kp.private_key),
        session_key=session,
        from_instance=derive_instance_id(kp.public_key),
        to_instance="peer",
        sig_suite="ed25519",
        event_type=FederationEventType.SPACE_MEDIA_BLOB,
        payload=payload,
        raw_chunk=raw,
        space_id="sp1",
    )
    return encode_media_body(header, sealed), header


def _service() -> FederationService:
    kp = generate_identity_keypair()
    return FederationService(
        db=MagicMock(),
        federation_repo=MagicMock(),
        outbox_repo=MagicMock(),
        key_manager=MagicMock(),
        bus=EventBus(),
        own_instance_id=derive_instance_id(kp.public_key),
        own_identity_seed=kp.private_key,
        own_identity_pk=kp.public_key,
        http_client=None,
    )


def _names(pipeline) -> list[str]:
    return [getattr(s, "__name__", "") for s in pipeline._steps]


def test_media_route_runs_the_full_inbox_chain_plus_an_event_type_gate():
    svc = _service()
    inbox = _names(svc._build_inbound_pipeline())
    media = _names(svc._build_media_https_pipeline())
    assert media == [inbox[0], "check_event_types", *inbox[1:]]
    for step in (
        "lookup_instance",
        "check_timestamp",
        "verify_signature",
        "check_replay",
        "decrypt_and_parse",
        "idempotency_check",
        "ban_check",
        "persist_replay",
    ):
        assert step in media


def test_media_route_takes_media_chunks_only():
    assert MEDIA_HTTPS_EVENT_TYPES == frozenset(
        {FederationEventType.SPACE_MEDIA_BLOB, FederationEventType.DM_MEDIA_BLOB}
    )


def test_only_routing_fields_travel_in_cleartext():
    session = os.urandom(32)
    raw = b"SECRET-CHUNK-" + os.urandom(256)
    body, header = _body(
        raw,
        session,
        {
            "filename": "holiday-photo.webp",
            "mime_type": "image/webp",
            "transfer_id": "transfer-xyz",
            "size_bytes": 987654,
            "chunk_index": 0,
            "chunk_count": 1,
        },
    )
    assert set(header) == _ROUTING_FIELDS
    for secret in (
        b"SECRET-CHUNK-",
        raw,
        b"holiday-photo",
        b"image/webp",
        b"transfer-xyz",
        b"987654",
        b"chunk_sha256",
        b"media_frame_suite",
        b"media_aead_suite",
        b"bytes_b64",
    ):
        assert secret not in body


def test_no_base64_of_the_chunk_on_the_wire():
    """The sealed chunk rides as raw bytes: a 512 KiB chunk is one chunk +
    GCM + the header, under the route cap — not ~1.8x."""
    session = os.urandom(32)
    raw = os.urandom(HTTPS_CHUNK_BYTES)
    body, _ = _body(raw, session, {"transfer_id": "t"})
    assert len(body) < len(raw) + 4096
    assert len(body) <= mf.HTTPS_MEDIA_MAX_BODY_BYTES


def test_signature_binds_the_binary_chunk():
    """``chunk_sha256`` sits in the signed, encrypted metadata."""
    session = os.urandom(32)
    raw = os.urandom(1000)
    kp = generate_identity_keypair()
    enc = FederationEncoder(kp.private_key)
    header, _sealed = build_media_envelope(
        encoder=enc,
        session_key=session,
        from_instance=derive_instance_id(kp.public_key),
        to_instance="peer",
        sig_suite="ed25519",
        event_type=FederationEventType.DM_MEDIA_BLOB,
        payload={},
        raw_chunk=raw,
        space_id=None,
    )
    signed = orjson.dumps({k: v for k, v in header.items() if k != "signatures"})
    assert header["encrypted_payload"].encode() in signed
    meta = orjson.loads(enc.decrypt_payload(header["encrypted_payload"], session))
    assert meta["chunk_sha256"] == b64url_encode(hashlib.sha256(raw).digest())
    assert meta["media_frame_suite"] in mf.SUPPORTED_MEDIA_FRAME_SUITES


def test_frame_suite_is_a_closed_set_with_a_rejection():
    assert mf.SUPPORTED_MEDIA_FRAME_SUITES == frozenset({mf.MEDIA_FRAME_SUITE_V1})
    assert issubclass(mf.UnsupportedMediaFrameSuite, ValueError)


def test_body_cap_is_one_chunk_plus_a_small_overhead():
    assert HTTPS_CHUNK_BYTES < mf.HTTPS_MEDIA_MAX_BODY_BYTES
    assert mf.HTTPS_MEDIA_MAX_BODY_BYTES - HTTPS_CHUNK_BYTES <= 16 * 1024


def test_a_media_body_is_recognised_from_the_body_alone():
    """Same inbox URL as JSON; a relay may rewrite Content-Type and drop
    headers, so the body's first bytes decide — and no JSON text starts
    with them."""
    body, _ = _body(b"x", os.urandom(32), {})
    assert mf.is_media_body(body)
    assert body[0] == 0
    for json_start in (b"{", b" ", b"\t", b"\n", b"\r", b"[", b'"'):
        assert not mf.is_media_body(json_start + body[1:])
