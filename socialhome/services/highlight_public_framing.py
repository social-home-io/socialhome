"""Framing protocol for public-content DataChannels.

This module serves BOTH the public-highlight flow (§highlights_public)
and the public-moments live index (§Momentum-public): the wire format,
chunking, and decoder are content-agnostic, so the only per-feature
difference is the opening meta frame's ``kind`` (``highlight_meta`` vs
``moment_index_meta``). The viewer's decoder is transport-blind and
feature-blind — it reassembles length-prefixed frames the same way no
matter which producer streamed them.

The public viewer talks to the author's instance over a single ordered
WebRTC DataChannel. Each frame on the wire is:

```
[u32 header_len BE][header_json][u32 payload_len BE][payload_bytes]
```

``header_json`` is a UTF-8 JSON object whose ``kind`` decides how the
``payload_bytes`` are interpreted. ``payload_len`` MAY be ``0`` for
control frames that carry no bytes.

Header kinds (v1):

- ``highlight_meta`` — emitted exactly once, at the start of the stream.
  Header carries the full Highlight dict + per-frame manifest. Payload
  is empty.
- ``frame_chunk`` — one binary chunk of a frame's media. Header
  carries ``{frame_id, sequence, chunk_index, is_last_chunk,
  byte_length}``. Payload is the raw bytes for that chunk.
- ``stream_end`` — emitted after the final ``frame_chunk``. Viewer
  closes the channel on receipt. No payload.
- ``author_profile`` — who shared it, right after ``highlight_meta``,
  only to a viewer that sent an ephemeral key: header ``{seal_suite, epk,
  nonce}``, payload the profile sealed to that key.
- ``error`` — emitted in place of any frame the author cannot serve.
  ``error`` field carries the reason string (``expired`` /
  ``unauthorized`` / ``backpressure``). Channel closes immediately
  after.

Both sides agree on these wire constants:
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass

import orjson

#: Maximum bytes per ``frame_chunk`` payload. Matches the chunk size
#: used by :class:`MediaServeView` so the disk-read pacing is aligned
#: across HTTP and DataChannel surfaces.
CHUNK_SIZE: int = 64 * 1024

#: High-water mark for ``RTCDataChannel.bufferedAmount``. Mirrors
#: ``federation/sync_rtc.SEND_HWM_BYTES``. Author-side waits for
#: ``buffered_amount_low`` before pushing the next chunk.
SEND_HWM_BYTES: int = 1 << 20

#: Wire label for the public-viewer DataChannel. Authoritative — both
#: the JS bootstrap and the server-side answerer agree on it byte-for-
#: byte so the channel's role can't be confused with sync DataChannels.
CHANNEL_LABEL: str = "highlight-public-v1"


# ─── Header kinds ──────────────────────────────────────────────────────


KIND_HIGHLIGHT_META: str = "highlight_meta"
KIND_MOMENT_INDEX_META: str = "moment_index_meta"
KIND_FRAME_CHUNK: str = "frame_chunk"
KIND_STREAM_END: str = "stream_end"
KIND_ERROR: str = "error"
#: Who shared the highlight, sealed to the viewer's ephemeral key
#: (:mod:`socialhome.services.highlight_public_profile`). Header:
#: ``{seal_suite, epk, nonce}``; payload: the AES-GCM ciphertext. Sent
#: once, right after ``highlight_meta``, only to a viewer that sent a key.
KIND_AUTHOR_PROFILE: str = "author_profile"

VALID_KINDS: frozenset[str] = frozenset(
    {
        KIND_HIGHLIGHT_META,
        KIND_MOMENT_INDEX_META,
        KIND_FRAME_CHUNK,
        KIND_STREAM_END,
        KIND_ERROR,
        KIND_AUTHOR_PROFILE,
    }
)


# ─── Errors ─────────────────────────────────────────────────────────────


class FramingError(ValueError):
    """Raised when the wire bytes don't match the spec.

    Mapped to ``error`` frames at the protocol boundary. Inheriting
    from :class:`ValueError` so the central exception mapper in
    :class:`BaseView._iter` returns 422 for any place this leaks.
    """

    __slots__ = ()


# ─── Encoder ────────────────────────────────────────────────────────────


def encode(header: dict, payload: bytes = b"") -> bytes:
    """Encode one wire frame.

    The header is canonicalised with sort-keyed JSON so the on-disk
    golden tests stay byte-stable. ``payload`` may be ``b""`` for
    control frames.
    """
    if header.get("kind") not in VALID_KINDS:
        raise FramingError(f"unknown kind: {header.get('kind')!r}")
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise FramingError("payload must be bytes")
    body = bytes(payload)
    # Canonical: sorted keys, compact, UTF-8. ``orjson.dumps`` already
    # returns UTF-8 bytes (no ``ensure_ascii`` escaping, always compact),
    # so ``OPT_SORT_KEYS`` reproduces the previous
    # ``json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False)``
    # output byte-for-byte.
    header_bytes = orjson.dumps(header, option=orjson.OPT_SORT_KEYS)
    return (
        struct.pack(">I", len(header_bytes))
        + header_bytes
        + struct.pack(">I", len(body))
        + body
    )


# ─── Decoder ────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class Frame:
    """A decoded wire frame."""

    header: dict
    payload: bytes


def decode(buf: bytes) -> Frame:
    """Decode exactly one wire frame from ``buf``.

    Raises :class:`FramingError` on truncation, oversize, or invalid
    header. Tail bytes after the declared payload are tolerated and
    silently discarded — the caller is responsible for tracking buffer
    boundaries via :func:`iter_frames` for streaming reassembly.
    """
    if len(buf) < 4:
        raise FramingError("buffer too short for header_len")
    header_len = struct.unpack(">I", buf[:4])[0]
    if header_len == 0:
        raise FramingError("zero-length header")
    if len(buf) < 4 + header_len + 4:
        raise FramingError("buffer too short for header_json + payload_len")
    header_bytes = buf[4 : 4 + header_len]
    try:
        header = orjson.loads(header_bytes)
    except orjson.JSONDecodeError as exc:
        raise FramingError(f"invalid header JSON: {exc}") from exc
    if not isinstance(header, dict):
        raise FramingError("header must be a JSON object")
    kind = header.get("kind")
    if kind not in VALID_KINDS:
        raise FramingError(f"unknown kind: {kind!r}")
    payload_len_off = 4 + header_len
    payload_len = struct.unpack(">I", buf[payload_len_off : payload_len_off + 4])[0]
    payload_off = payload_len_off + 4
    if len(buf) < payload_off + payload_len:
        raise FramingError("buffer too short for declared payload_len")
    payload = bytes(buf[payload_off : payload_off + payload_len])
    return Frame(header=header, payload=payload)


def iter_frames(buf: bytes) -> Iterator[Frame]:
    """Yield every complete frame in ``buf``.

    Tail bytes (an in-flight partial frame) raise :class:`FramingError`.
    Callers that need streaming reassembly should keep their own
    buffer and only call :func:`iter_frames` once they've appended a
    full frame's worth of bytes.
    """
    off = 0
    n = len(buf)
    while off < n:
        if n - off < 4:
            raise FramingError("trailing partial frame (header_len)")
        header_len = struct.unpack(">I", buf[off : off + 4])[0]
        end_header = off + 4 + header_len
        if n < end_header + 4:
            raise FramingError("trailing partial frame (header / payload_len)")
        payload_len = struct.unpack(">I", buf[end_header : end_header + 4])[0]
        end_frame = end_header + 4 + payload_len
        if n < end_frame:
            raise FramingError("trailing partial frame (payload)")
        yield decode(buf[off:end_frame])
        off = end_frame


# ─── Convenience builders ──────────────────────────────────────────────


def highlight_meta(highlight: dict, frames: list[dict]) -> bytes:
    """Build the opening ``highlight_meta`` frame."""
    return encode(
        {"kind": KIND_HIGHLIGHT_META, "highlight": highlight, "frames": frames}
    )


def moment_index_meta(moments: list[dict]) -> bytes:
    """Build the opening ``moment_index_meta`` frame for §Momentum-public.

    Mirrors :func:`highlight_meta`: the header carries the per-moment
    manifest (one dict per public moment) and the payload is empty. An
    empty ``moments`` list is valid — the viewer renders an empty index.
    """
    return encode({"kind": KIND_MOMENT_INDEX_META, "moments": moments})


def frame_chunk(
    *,
    frame_id: str,
    sequence: int,
    chunk_index: int,
    byte_length: int,
    is_last_chunk: bool,
    payload: bytes,
) -> bytes:
    """Build one ``frame_chunk``."""
    return encode(
        {
            "kind": KIND_FRAME_CHUNK,
            "frame_id": frame_id,
            "sequence": sequence,
            "chunk_index": chunk_index,
            "byte_length": byte_length,
            "is_last_chunk": is_last_chunk,
        },
        payload,
    )


def author_profile(header_fields: dict, ciphertext: bytes) -> bytes:
    """Build the sealed ``author_profile`` frame."""
    return encode({**header_fields, "kind": KIND_AUTHOR_PROFILE}, ciphertext)


def stream_end() -> bytes:
    """Build the terminating ``stream_end`` frame."""
    return encode({"kind": KIND_STREAM_END})


def error_frame(reason: str) -> bytes:
    """Build an ``error`` frame. ``reason`` is one of the protocol's
    reserved tags (``expired`` / ``unauthorized`` / ``backpressure``)."""
    return encode({"kind": KIND_ERROR, "error": reason})
