"""Tests for the signed media envelope shared by both binary media transports."""

from __future__ import annotations

import hashlib
import os

import orjson

from socialhome.crypto import (
    b64url_decode,
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    verify_ed25519,
)
from socialhome.domain.federation import FederationEventType
from socialhome.federation import media_framing as mf
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.media_envelope import (
    build_media_envelope,
    encode_media_body,
)


def _build(raw: bytes = b"chunk bytes", **kw):
    kp = generate_identity_keypair()
    enc = FederationEncoder(kp.private_key)
    session = os.urandom(32)
    header, sealed = build_media_envelope(
        encoder=enc,
        session_key=session,
        from_instance=derive_instance_id(kp.public_key),
        to_instance="peer",
        sig_suite="ed25519",
        event_type=FederationEventType.DM_MEDIA_BLOB,
        payload=kw.get(
            "payload",
            {
                "media_blob_id": "blob-correlation-id",
                "message_id": "message-correlation-id",
                "filename": "x.webp",
            },
        ),
        raw_chunk=raw,
        space_id=kw.get("space_id"),
    )
    return kp, enc, session, header, sealed


def test_header_carries_routing_fields_only_in_plaintext():
    _kp, _enc, _s, header, _sealed = _build(space_id="sp1")
    assert set(header) == {
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
    flat = orjson.dumps(header)
    # Encryption-first: no metadata leaks into the plaintext header.
    for secret in (
        b"x.webp",
        b"blob-correlation-id",
        b"message-correlation-id",
        b"chunk_sha256",
        b"media_frame_suite",
    ):
        assert secret not in flat


def test_metadata_binds_the_chunk_and_names_both_suites():
    raw = os.urandom(4096)
    _kp, enc, session, header, sealed = _build(raw)
    meta = orjson.loads(enc.decrypt_payload(header["encrypted_payload"], session))
    assert meta["chunk_sha256"] == b64url_encode(hashlib.sha256(raw).digest())
    assert meta["media_aead_suite"] == mf.MEDIA_AEAD_SUITE_AESGCM_256
    assert meta["media_frame_suite"] == mf.MEDIA_FRAME_SUITE_V1
    assert meta["filename"] == "x.webp"
    assert enc.decrypt_bytes(sealed, session) == raw


def test_signature_covers_the_canonical_envelope():
    kp, _enc, _s, header, _sealed = _build()
    signed = {k: v for k, v in header.items() if k != "signatures"}
    sig = header["signatures"]["ed25519"]
    assert verify_ed25519(kp.public_key, orjson.dumps(signed), b64url_decode(sig))


def test_sealed_chunk_is_raw_bytes_not_base64():
    raw = b"\x00\xff" * 1000
    _kp, _enc, _s, _header, sealed = _build(raw)
    assert len(sealed) == 12 + len(raw) + 16


def test_encode_media_body_is_one_decodable_frame():
    _kp, _enc, _s, header, sealed = _build()
    body = encode_media_body(header, sealed)
    assert mf.is_media_body(body)
    suite, frame = mf.decode_body(body)
    assert suite == mf.MEDIA_FRAME_SUITE_V1
    assert frame.header == orjson.dumps(header)  # the signed bytes verbatim
    assert frame.payload == sealed
