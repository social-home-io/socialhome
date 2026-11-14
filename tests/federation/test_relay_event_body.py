"""How a space event's payload rides as a binary relay frame's bytes: the
JSON suite, and a §25.6 sync chunk with its record ciphertext raw."""

from __future__ import annotations

import os

import orjson
import pytest

from socialhome.crypto import b64url_encode
from socialhome.domain.federation import FederationEventType
from socialhome.federation.relay_event_body import (
    EVENT_BODY_SUITE_JSON,
    EVENT_BODY_SUITE_SYNC_CHUNK_RAW,
    SUPPORTED_EVENT_BODY_SUITES,
    UnsupportedEventBodySuite,
    pack_event_body,
    unpack_event_body,
)

CHUNK = FederationEventType.SPACE_SYNC_CHUNK
PAGE = FederationEventType.SPACE_PAGE_CREATED


def _chunk_payload(ciphertext_bytes: int = 300_000) -> dict:
    """A ``SPACE_SYNC_CHUNK`` payload as the provider builds it."""
    sealed = (
        b64url_encode(os.urandom(12))
        + ":"
        + b64url_encode(os.urandom(ciphertext_bytes))
    )
    envelope = {
        "sync_id": "sync-1",
        "resource": "pages",
        "space_id": "space-1",
        "epoch": 3,
        "seq_start": 0,
        "seq_end": 1,
        "is_last": False,
        "encrypted_payload": sealed,
        "signatures": {"ed25519": "sig"},
    }
    return {"sync_id": "sync-1", "chunk": orjson.dumps(envelope).decode()}


def test_a_sync_chunk_rides_with_its_ciphertext_raw_and_comes_back_identical():
    payload = _chunk_payload()
    as_json = orjson.dumps(payload)
    suite, raw = pack_event_body(CHUNK, as_json.decode())
    assert suite == EVENT_BODY_SUITE_SYNC_CHUNK_RAW
    # No base64: about a quarter smaller than the JSON form.
    assert len(raw) < len(as_json) * 0.76
    assert unpack_event_body(suite, raw) == payload
    # Byte-identical chunk envelope: its own signature covers the JSON form.
    assert unpack_event_body(suite, raw)["chunk"] == payload["chunk"]


def test_any_other_event_is_its_json():
    payload = {"space_id": "s", "content": "x" * 1000}
    suite, raw = pack_event_body(PAGE, orjson.dumps(payload).decode())
    assert suite == EVENT_BODY_SUITE_JSON and raw == orjson.dumps(payload)
    assert unpack_event_body(suite, raw) == payload


@pytest.mark.parametrize(
    "payload",
    [
        {"sync_id": "s", "chunk": "not json"},
        {"sync_id": "s", "chunk": orjson.dumps({"resource": "pages"}).decode()},
        {
            "sync_id": "s",
            "chunk": orjson.dumps({"encrypted_payload": "not base64 !!"}).decode(),
        },
        # Not the canonical serialisation: would not come back identical.
        {"sync_id": "s", "chunk": '{"encrypted_payload" : "QUJD"}'},
        {"sync_id": "s"},
    ],
)
def test_a_chunk_that_would_not_round_trip_stays_json(payload):
    suite, raw = pack_event_body(CHUNK, orjson.dumps(payload).decode())
    assert suite == EVENT_BODY_SUITE_JSON
    assert unpack_event_body(suite, raw) == payload


def test_an_unknown_suite_is_refused_never_defaulted():
    assert SUPPORTED_EVENT_BODY_SUITES == {"json", "sync-chunk-raw-v1"}
    for suite in (None, "", "json-v2", 1):
        with pytest.raises(UnsupportedEventBodySuite):
            unpack_event_body(suite, b"{}")


@pytest.mark.parametrize("raw", [b"not json", b"[1]", b'"text"'])
def test_json_bytes_that_are_no_object_are_refused(raw):
    with pytest.raises(ValueError, match="not JSON|not a JSON object"):
        unpack_event_body(EVENT_BODY_SUITE_JSON, raw)


def _raw_frame(meta: object, body: bytes = b"") -> bytes:
    encoded = orjson.dumps(meta)
    return len(encoded).to_bytes(4, "big") + encoded + body


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\x00\x00",
        (10_000).to_bytes(4, "big") + b"{}",
        (3).to_bytes(4, "big") + b"nop",
        _raw_frame([1]),
        _raw_frame({"payload": {"chunk": "text"}, "parts": [0]}),
        _raw_frame({"payload": {"chunk": {"encrypted_payload": "x"}}, "parts": [0]}),
        _raw_frame({"payload": {"chunk": {"encrypted_payload": ""}}, "parts": []}),
        _raw_frame({"payload": {"chunk": {"encrypted_payload": ""}}, "parts": [5]}),
        _raw_frame(
            {"payload": {"chunk": {"encrypted_payload": ""}}, "parts": [-1, 4]}, b"abc"
        ),
        _raw_frame(
            {"payload": {"chunk": {"encrypted_payload": ""}}, "parts": [True]}, b"a"
        ),
        _raw_frame(
            {"payload": {"chunk": {"encrypted_payload": ""}}, "parts": [1] * 9},
            b"a" * 9,
        ),
        _raw_frame(
            {"payload": {"chunk": {"encrypted_payload": ""}}, "parts": [2]}, b"abc"
        ),
    ],
)
def test_a_malformed_raw_sync_chunk_is_refused(raw):
    with pytest.raises(ValueError):
        unpack_event_body(EVENT_BODY_SUITE_SYNC_CHUNK_RAW, raw)
