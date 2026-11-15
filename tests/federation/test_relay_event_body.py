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
    EVENT_BODY_SUITE_ROUTED_RAW,
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
    assert unpack_event_body(suite, raw, event_type=CHUNK) == payload
    # Byte-identical chunk envelope: its own signature covers the JSON form.
    assert unpack_event_body(suite, raw, event_type=CHUNK)["chunk"] == payload["chunk"]


def test_any_other_event_is_its_json():
    payload = {"space_id": "s", "content": "x" * 1000}
    suite, raw = pack_event_body(PAGE, orjson.dumps(payload).decode())
    assert suite == EVENT_BODY_SUITE_JSON and raw == orjson.dumps(payload)
    assert unpack_event_body(suite, raw, event_type=CHUNK) == payload


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
    assert unpack_event_body(suite, raw, event_type=CHUNK) == payload


def test_an_unknown_suite_is_refused_never_defaulted():
    assert SUPPORTED_EVENT_BODY_SUITES == {
        "json",
        "sync-chunk-raw-v1",
        "space-routed-raw-v1",
    }
    for suite in (None, "", "json-v2", 1):
        with pytest.raises(UnsupportedEventBodySuite):
            unpack_event_body(suite, b"{}", event_type=PAGE)


@pytest.mark.parametrize("raw", [b"not json", b"[1]", b'"text"'])
def test_json_bytes_that_are_no_object_are_refused(raw):
    with pytest.raises(ValueError, match="not JSON|not a JSON object"):
        unpack_event_body(EVENT_BODY_SUITE_JSON, raw, event_type=PAGE)


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
        unpack_event_body(EVENT_BODY_SUITE_SYNC_CHUNK_RAW, raw, event_type=CHUNK)


def _nested(depth: int) -> object:
    value: object = "x"
    for _ in range(depth):
        value = [value]
    return value


def test_a_raw_sync_chunk_nested_too_deep_is_refused_as_a_value_error():
    """``orjson.dumps`` raises a ``TypeError`` past 254 levels: the refusal
    must still be the ``ValueError`` every caller (and the relay's junk
    budget) counts."""
    meta = b'{"payload":{"chunk":{"encrypted_payload":"","x":'
    meta += b"[" * 300 + b"1" + b"]" * 300 + b'}},"parts":[1]}'
    raw = len(meta).to_bytes(4, "big") + meta + b"a"
    with pytest.raises(ValueError):
        unpack_event_body(EVENT_BODY_SUITE_SYNC_CHUNK_RAW, raw, event_type=CHUNK)


def test_a_chunk_nested_too_deep_to_pack_stays_json():
    envelope = '{"encrypted_payload":"QUJD","x":' + "[" * 252 + "1" + "]" * 252 + "}"
    payload_json = orjson.dumps({"sync_id": "s", "chunk": envelope}).decode()
    # Sanity: the chunk string itself is what the packer would re-serialise.
    suite, raw = pack_event_body(CHUNK, payload_json)
    assert suite == EVENT_BODY_SUITE_JSON and raw == payload_json.encode()


def test_the_raw_suite_is_for_sync_chunks_only():
    """The suite is bound to the event type: a page (or anything else)
    claiming the raw layout is refused before its bytes are read."""
    payload = _chunk_payload(1000)
    suite, raw = pack_event_body(CHUNK, orjson.dumps(payload).decode())
    assert suite == EVENT_BODY_SUITE_SYNC_CHUNK_RAW
    assert unpack_event_body(suite, raw, event_type=CHUNK) == payload
    for other in (PAGE, FederationEventType.SPACE_ROSTER_SNAPSHOT):
        with pytest.raises(UnsupportedEventBodySuite):
            unpack_event_body(suite, raw, event_type=other)


# ── A SPACE_ROUTED wrapper with its sealed ciphertext raw (host hop) ─────

ROUTED = FederationEventType.SPACE_ROUTED


def _routed_payload(ciphertext_bytes: int = 300_000, **sealed_extra) -> dict:
    """A ``SPACE_ROUTED`` payload as ``SpaceRoutedHandler.send_routed``
    builds it: routing fields plus the sealed blob, its ciphertext b64url."""
    return {
        "route_id": "r" * 32,
        "path": ["a" * 32, "h" * 32, "b" * 32],
        "position": 0,
        "direction": "forward",
        "inner_event_type": "space_page_updated",
        "sealed": {
            "kem_suite": "x25519",
            "origin_eph_pk": "o" * 43,
            "target_eph_pk": "t" * 43,
            "nonce": b64url_encode(os.urandom(12)),
            "ciphertext": b64url_encode(os.urandom(ciphertext_bytes)),
            "origin_identity_pk": "p" * 64,
            "origin_sig_suite": "ed25519",
            "origin_sig": "s" * 86,
            **sealed_extra,
        },
    }


def test_a_routed_wrapper_rides_with_its_sealed_ciphertext_raw():
    """The host hop's leg toward a household behind a GFS: the sealed inner
    is already ciphertext, so it rides the frame raw, not base64'd."""
    payload = _routed_payload()
    as_json = orjson.dumps(payload)
    suite, raw = pack_event_body(ROUTED, as_json.decode())
    assert suite == EVENT_BODY_SUITE_ROUTED_RAW
    assert suite in SUPPORTED_EVENT_BODY_SUITES
    assert len(raw) < len(as_json) * 0.76
    assert unpack_event_body(suite, raw, event_type=ROUTED) == payload


def test_a_routed_wrapper_around_a_raw_body_keeps_its_suite():
    payload = _routed_payload(event_body_suite=EVENT_BODY_SUITE_SYNC_CHUNK_RAW)
    suite, raw = pack_event_body(ROUTED, orjson.dumps(payload).decode())
    assert suite == EVENT_BODY_SUITE_ROUTED_RAW
    assert unpack_event_body(suite, raw, event_type=ROUTED) == payload


def _framed(meta: dict, body: bytes) -> bytes:
    raw_meta = orjson.dumps(meta)
    return len(raw_meta).to_bytes(4, "big") + raw_meta + body


@pytest.mark.parametrize(
    "payload",
    [
        {"route_id": "r", "sealed": "not-a-dict"},
        {"route_id": "r", "sealed": {"ciphertext": "!!not base64!!"}},
        {"route_id": "r", "sealed": {"nonce": "n"}},
        {"route_id": "r"},
    ],
)
def test_a_routed_wrapper_that_would_not_round_trip_stays_json(payload):
    suite, raw = pack_event_body(ROUTED, orjson.dumps(payload).decode())
    assert suite == EVENT_BODY_SUITE_JSON
    assert orjson.loads(raw) == payload


def test_the_routed_raw_suite_is_for_routed_wrappers_only():
    _suite, raw = pack_event_body(ROUTED, orjson.dumps(_routed_payload()).decode())
    for event_type in (PAGE, CHUNK):
        with pytest.raises(UnsupportedEventBodySuite):
            unpack_event_body(EVENT_BODY_SUITE_ROUTED_RAW, raw, event_type=event_type)
    _suite, chunk_raw = pack_event_body(CHUNK, orjson.dumps(_chunk_payload()).decode())
    with pytest.raises(UnsupportedEventBodySuite):
        unpack_event_body(EVENT_BODY_SUITE_SYNC_CHUNK_RAW, chunk_raw, event_type=ROUTED)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\x00\x00\x00\xff{}",
        b"\x00\x00\x00\x02{}",
        # The sealed blob without its emptied ciphertext slot.
        _framed({"payload": {"sealed": {}}, "parts": [1]}, b"x"),
        # A ciphertext slot that was not emptied (a second, JSON copy).
        _framed({"payload": {"sealed": {"ciphertext": "x"}}, "parts": [1]}, b"x"),
        # Part lengths that do not add up to the bytes after the meta.
        _framed({"payload": {"sealed": {"ciphertext": ""}}, "parts": [5]}, b"x"),
        # More than one part: a routed wrapper carries one ciphertext.
        _framed({"payload": {"sealed": {"ciphertext": ""}}, "parts": [1, 1]}, b"xy"),
        # No sealed blob at all.
        _framed({"payload": {"route_id": "r"}, "parts": [1]}, b"x"),
    ],
)
def test_a_malformed_raw_routed_wrapper_is_refused(raw):
    with pytest.raises(ValueError):
        unpack_event_body(EVENT_BODY_SUITE_ROUTED_RAW, raw, event_type=ROUTED)
