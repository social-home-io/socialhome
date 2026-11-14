"""How a space event's payload rides as the bytes of a binary relay frame.

A space event too large for a JSON envelope on the GFS relay goes to a
household behind a GFS as one binary relay frame
(:meth:`~socialhome.federation.federation_service.FederationService
.relay_event_frame`): the signed header names the event, the frame's bytes
carry its payload. This module is the one place that says how — a **body
suite**, named in the signed header metadata as ``event_body_suite`` and
required by the receiver (an unknown one is refused, never defaulted):

* :data:`EVENT_BODY_SUITE_JSON` — the bytes are the payload's JSON.
* :data:`EVENT_BODY_SUITE_SYNC_CHUNK_RAW` — for a §25.6
  ``SPACE_SYNC_CHUNK``. Its payload is ``{sync_id, chunk}``, ``chunk`` the
  serialised chunk envelope whose ``encrypted_payload`` holds the records as
  base64url text (``nonce:ciphertext``): a third larger than the
  ciphertext, which would make the largest record a catch-up carries a
  third smaller than the largest event that travels live. Here the
  ciphertext rides **raw**::

      u32 BE meta length | meta JSON | raw part 1 | raw part 2 | …

  ``meta`` is ``{"payload": <the payload, its chunk parsed and its
  encrypted_payload emptied>, "parts": [<byte length of each raw part>]}``.
  The receiver re-encodes the parts and rebuilds the very same payload (the
  sender checks that round trip before it picks this suite, and falls back
  to JSON otherwise), so the chunk envelope's own signature and AEAD — over
  its JSON form — verify unchanged. Nothing about the §25.6 stream changes;
  only how one chunk event is laid out inside a relay frame.

Pure: no I/O.
"""

from __future__ import annotations

import struct

import orjson

from ..crypto import b64url_decode, b64url_encode
from ..domain.errors import WrongTypeError
from ..domain.federation import FederationEventType

#: The frame's bytes are the payload's JSON.
EVENT_BODY_SUITE_JSON: str = "json"
#: A sync chunk with its record ciphertext as raw bytes (see the module doc).
EVENT_BODY_SUITE_SYNC_CHUNK_RAW: str = "sync-chunk-raw-v1"
SUPPORTED_EVENT_BODY_SUITES: frozenset[str] = frozenset(
    {EVENT_BODY_SUITE_JSON, EVENT_BODY_SUITE_SYNC_CHUNK_RAW}
)
#: The signed header metadata field that names the suite.
EVENT_BODY_SUITE_FIELD: str = "event_body_suite"

_LEN = struct.Struct(">I")
_CHUNK = "chunk"
_SEALED = "encrypted_payload"
#: Most ``:``-separated parts an ``encrypted_payload`` may have.
_MAX_PARTS = 4


class UnsupportedEventBodySuite(ValueError):
    """A framed event names a body suite this build does not read."""


def pack_event_body(
    event_type: FederationEventType, payload_json: str
) -> tuple[str, bytes]:
    """``(suite, bytes)`` for *payload_json* as a relay frame's bytes: a
    sync chunk with its ciphertext raw when that round-trips exactly, else
    the JSON itself."""
    if event_type is FederationEventType.SPACE_SYNC_CHUNK:
        packed = _pack_sync_chunk(payload_json)
        if packed is not None:
            return EVENT_BODY_SUITE_SYNC_CHUNK_RAW, packed
    return EVENT_BODY_SUITE_JSON, payload_json.encode("utf-8")


def unpack_event_body(suite: object, raw: bytes) -> dict:
    """The payload a frame's bytes carry under *suite*. Raises
    :class:`UnsupportedEventBodySuite` for an unknown suite (no default) and
    :class:`ValueError` for bytes that are not a payload object."""
    if suite not in SUPPORTED_EVENT_BODY_SUITES:
        raise UnsupportedEventBodySuite(f"unknown event_body_suite: {suite!r}")
    if suite == EVENT_BODY_SUITE_SYNC_CHUNK_RAW:
        return _unpack_sync_chunk(raw)
    try:
        payload = orjson.loads(raw)
    except Exception as exc:
        raise ValueError("framed event payload is not JSON") from exc
    if not isinstance(payload, dict):
        raise WrongTypeError("framed event payload is not a JSON object")
    return payload


def _pack_sync_chunk(payload_json: str) -> bytes | None:
    """The raw layout of a sync chunk payload, or ``None`` when it is not
    the expected shape or would not come back byte-identical."""
    try:
        payload = orjson.loads(payload_json)
        chunk = payload.get(_CHUNK) if isinstance(payload, dict) else None
        envelope = orjson.loads(chunk) if isinstance(chunk, str) else None
        sealed = envelope.get(_SEALED) if isinstance(envelope, dict) else None
        if not isinstance(sealed, str) or not sealed:
            return None
        parts = [b64url_decode(p) for p in sealed.split(":")]
    except ValueError, TypeError, AttributeError:
        return None
    if not isinstance(envelope, dict) or len(parts) > _MAX_PARTS:
        return None
    meta = orjson.dumps(
        {
            "payload": {**payload, _CHUNK: {**envelope, _SEALED: ""}},
            "parts": [len(p) for p in parts],
        }
    )
    packed = _LEN.pack(len(meta)) + meta + b"".join(parts)
    try:
        if _unpack_sync_chunk(packed) != payload:
            return None
    except ValueError:
        return None
    return packed


def _unpack_sync_chunk(raw: bytes) -> dict:
    if len(raw) < _LEN.size:
        raise ValueError("framed sync chunk is too short")
    (meta_len,) = _LEN.unpack_from(raw)
    body_at = _LEN.size + meta_len
    if body_at > len(raw):
        raise ValueError("framed sync chunk meta length past the frame")
    try:
        meta = orjson.loads(raw[_LEN.size : body_at])
    except Exception as exc:
        raise ValueError("framed sync chunk meta is not JSON") from exc
    payload = meta.get("payload") if isinstance(meta, dict) else None
    sizes = meta.get("parts") if isinstance(meta, dict) else None
    envelope = payload.get(_CHUNK) if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or not isinstance(envelope, dict)
        or envelope.get(_SEALED) != ""
        or not isinstance(sizes, list)
        or not 1 <= len(sizes) <= _MAX_PARTS
        or not all(
            isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in sizes
        )
        or sum(sizes) != len(raw) - body_at
    ):
        raise WrongTypeError("framed sync chunk is malformed")
    parts: list[str] = []
    at = body_at
    for size in sizes:
        parts.append(b64url_encode(raw[at : at + size]))
        at += size
    return {
        **payload,
        _CHUNK: orjson.dumps({**envelope, _SEALED: ":".join(parts)}).decode(),
    }


__all__ = [
    "EVENT_BODY_SUITE_FIELD",
    "EVENT_BODY_SUITE_JSON",
    "EVENT_BODY_SUITE_SYNC_CHUNK_RAW",
    "SUPPORTED_EVENT_BODY_SUITES",
    "UnsupportedEventBodySuite",
    "pack_event_body",
    "unpack_event_body",
]
