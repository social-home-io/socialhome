"""Tests for :mod:`socialhome.domain.ws_binary_frame` (``ws_binary1``)."""

from __future__ import annotations

import os
import struct

import orjson
import pytest

from socialhome.authority_sig import (
    sign_authority_event,
    strip_authority_sig_fields,
    verify_authority_event,
)
from socialhome.crypto import b64url_encode, generate_space_keypair
from socialhome.domain.ws_binary_frame import (
    BIN_PATH_FIELD,
    BINARY_CIPHERTEXT_PATHS,
    MIN_BINARY_CIPHERTEXT_CHARS,
    WS_BINARY_VERSION,
    InvalidBinaryFrame,
    decode_binary_frame,
    encode_binary_frame,
)

CT = f"{b64url_encode(os.urandom(12))}:{b64url_encode(os.urandom(1040))}"


def _relay_frame(ct: str = CT) -> dict:
    return {
        "type": "relay",
        "space_id": "sp-1",
        "event_type": "space_post_public",
        "payload": {
            "space_id": "sp-1",
            "epoch": 7,
            "encrypted_payload": ct,
            "payload_suite": "zlib1",
            "authority_sig": "c2ln",
            "authority_sig_suite": "ed25519",
        },
    }


#: Every frame type the list names, with a realistic ciphertext field.
_FRAMES = {
    "relay authority": _relay_frame(),
    "relay space_item": {
        "type": "relay",
        "space_id": "sp-1",
        "event_type": "space_item",
        "epoch": 7,
        "writer_cert": {"cert_sig": "x"},
        "payload": CT,
    },
    "relay key handoff": {
        "type": "relay",
        "space_id": "sp-1",
        "event_type": "space_author_profiles_bundle",
        "payload": {"space_id": "sp-1", "sealed": {"kem_suite": "x", "ciphertext": CT}},
    },
    "envelope": {
        "type": "envelope",
        "sealed": {"kem_suite": "x25519", "eph_pk": "ZXBo", "ciphertext": CT},
    },
    "moment": {
        "type": "incoming_public_moment",
        "payload": {"moment_id": "m", "ciphertext": CT, "signature": "c2ln"},
    },
}


@pytest.mark.parametrize("name", sorted(_FRAMES))
def test_every_listed_frame_round_trips_exactly(name):
    frame = _FRAMES[name]
    raw = encode_binary_frame(frame)
    assert raw is not None
    assert decode_binary_frame(raw) == frame
    # The ciphertext travels raw: a quarter smaller than its base64 text.
    assert len(raw) < len(orjson.dumps(frame)) - 1040 // 4


def test_the_layout_is_version_length_header_body():
    raw = encode_binary_frame(_relay_frame())
    version, header_len = struct.unpack_from(">BH", raw)
    assert version == WS_BINARY_VERSION == 1
    header = orjson.loads(raw[3 : 3 + header_len])
    assert header[BIN_PATH_FIELD] == ["payload", "encrypted_payload"]
    # The nonce part stays in the header; only the long part goes raw.
    nonce, _ = CT.split(":")
    assert header["payload"]["encrypted_payload"] == nonce + ":"
    assert len(raw) - 3 - header_len == 1040  # the raw ciphertext bytes


def test_the_frame_list_is_explicit():
    """Tripwire: a new frame type carrying a ciphertext is added here on
    purpose (and gets a round-trip case above)."""
    assert BINARY_CIPHERTEXT_PATHS == {
        "relay": (
            ("payload", "encrypted_payload"),
            ("payload", "sealed", "ciphertext"),
            ("payload",),
        ),
        "envelope": (("sealed", "ciphertext"),),
        "incoming_public_moment": (("payload", "ciphertext"),),
    }


def test_the_caller_frame_is_never_mutated():
    frame = _relay_frame()
    before = orjson.dumps(frame)
    encode_binary_frame(frame)
    assert orjson.dumps(frame) == before


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "new_subscriber", "payload": CT},  # unlisted type
        {"type": "relay", "payload": {"other": CT}},  # no listed field
        _relay_frame("short:abc"),  # too short to pay off
        _relay_frame(CT + "="),  # padded: not canonical
        _relay_frame(CT[:-1] + "!"),  # not base64url
        {**_relay_frame(), BIN_PATH_FIELD: ["x"]},  # would collide
    ],
)
def test_frames_that_go_as_text(frame):
    assert encode_binary_frame(frame) is None


def test_non_canonical_base64_is_never_cut():
    """A ciphertext whose base64 has stray trailing bits would re-encode to
    different text and break the signature over it — it goes as text."""
    # 301 bytes: the last character carries 2 data bits and 4 zero bits.
    body = b64url_encode(os.urandom(301))
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    tweaked = body[:-1] + alphabet[alphabet.index(body[-1]) | 1]
    assert encode_binary_frame(_relay_frame("n:" + tweaked)) is None


def test_a_signature_over_the_rebuilt_frame_still_verifies():
    """The follower rebuilds the exact string, so the authority signature the
    seed holder made over the text form verifies on the binary path."""
    skp = generate_space_keypair()
    envelope = {"space_id": "sp-1", "epoch": 7, "encrypted_payload": CT}
    envelope.update(
        sign_authority_event(
            event_type="space_post_public",
            space_id="sp-1",
            payload=envelope,
            space_seed=skp.private_key,
        )
    )
    frame = {
        "type": "relay",
        "space_id": "sp-1",
        "event_type": "space_post_public",
        "payload": envelope,
    }
    got = decode_binary_frame(encode_binary_frame(frame))["payload"]
    assert verify_authority_event(
        event_type="space_post_public",
        space_id="sp-1",
        payload=strip_authority_sig_fields(got),
        authority_sig=got["authority_sig"],
        authority_sig_suite=got["authority_sig_suite"],
        space_public_key=skp.public_key,
    )


def _wrap(header: object, body: bytes = b"\x00" * 10, *, version: int = 1) -> bytes:
    raw = orjson.dumps(header)
    return struct.pack(">BH", version, len(raw)) + raw + body


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x01\x00",
        struct.pack(">BH", 2, 2) + b"{}",
        struct.pack(">BH", 1, 500) + b"{}",
        struct.pack(">BH", 1, 3) + b"{x}",
        _wrap([1, 2]),
        _wrap({"type": "relay"}),
        _wrap({"type": "relay", BIN_PATH_FIELD: []}),
        _wrap({"type": "relay", BIN_PATH_FIELD: ["a"] * 5}),
        _wrap({"type": "relay", BIN_PATH_FIELD: [1]}),
        _wrap({"type": "relay", BIN_PATH_FIELD: ["payload"]}),
        _wrap({"type": "relay", "payload": {"x": 1}, BIN_PATH_FIELD: ["payload"]}),
        _wrap(
            {"type": "relay", "payload": "n:", BIN_PATH_FIELD: ["payload"]}, version=9
        ),
    ],
)
def test_a_malformed_binary_frame_is_refused(data):
    with pytest.raises(InvalidBinaryFrame):
        decode_binary_frame(data)


def test_min_length_constant():
    assert MIN_BINARY_CIPHERTEXT_CHARS == 256
