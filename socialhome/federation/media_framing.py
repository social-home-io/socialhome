"""Binary framing for the federation media DataChannel (``fed-media-v1``).

The federation PeerConnection carries two DataChannels (§24.12):

* ``fed-v1`` — small, latency-sensitive control + routine envelopes,
  serialised as UTF-8 JSON (``federation/transport.py``).
* ``fed-media-v1`` — bulk media chunks (DM + space blobs) as **binary
  frames with no base64**, so a 200 MiB video doesn't head-of-line-block
  a typing indicator on the control channel and doesn't pay the ~37 %
  base64 tax.

Each frame on the wire is::

    [u8 frame_type][u32 header_len BE][header_bytes][u32 payload_len BE][payload_bytes]

* ``frame_type`` discriminates frame kinds so the *same* versioned
  channel can grow new frame shapes (flow-control, resume tokens, …)
  without a new channel label or a protocol-version bump. v1 ships only
  :data:`FRAME_TYPE_MEDIA_CHUNK`; an older receiver skips an unknown
  type rather than erroring (forward-compatible).
* ``header_bytes`` is **opaque** to this module — the media transport
  puts the signed federation envelope JSON here verbatim, so the exact
  bytes that were signed are the exact bytes the §24.11 pipeline
  re-parses. This module never parses or re-serialises the header (that
  would risk a signed-bytes mismatch).
* ``payload_bytes`` is the AES-256-GCM-encrypted chunk
  (``nonce || ciphertext``). MAY be empty for control frames.

Reassembly: SCTP preserves message boundaries, so one ``channel.send``
normally arrives as one whole frame. :func:`iter_complete_frames`
nonetheless tolerates a peer that coalesces or splits messages — it
extracts every complete frame from a running buffer and returns the
unconsumed tail.

The same frame also rides the HTTPS inbox (proto v_81) — POSTed to the
very inbox URL the peer advertised (``…/federation/inbox/{inbox_id}``, or
Home Assistant's ``/api/socialhome/inbox/{inbox_id}`` relay in front of
it), so nothing between the two households needs to know a new path. The
body tells the receiver what it is, never a header (a relay may rewrite
``Content-Type`` and drops custom headers)::

    [MEDIA_BODY_MAGIC "\x00SHM"][u8 frame-suite id][one frame as above]

``\x00`` can never start (or appear outside a string in) a JSON envelope,
so a JSON body is never mistaken for a frame and vice versa
(:func:`is_media_body`). The suite byte names the frame format
(:data:`MEDIA_FRAME_SUITE_IDS`); :func:`decode_body` refuses an unknown one
(:class:`UnsupportedMediaFrameSuite`), trailing bytes and any frame type
but :data:`FRAME_TYPE_MEDIA_CHUNK` (a body has no stream to skip forward
in), under the cap :data:`HTTPS_MEDIA_MAX_BODY_BYTES`.

This is a deliberately separate module from
:mod:`socialhome.services.highlight_public_framing`: that one
multiplexes several JSON-header *kinds* on one channel and canonicalises
the header with sort-keyed JSON; here the header must stay byte-exact
for signature verification, so the concerns don't share code.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ..domain.media_chunking import FRAME_OVERHEAD_BYTES, HTTPS_CHUNK_BYTES

# ─── Wire constants ──────────────────────────────────────────────────────

#: DataChannel label for the binary media stream. Versioned — a
#: breaking frame-format change bumps to ``fed-media-v2`` rather than
#: silently reinterpreting bytes.
CHANNEL_LABEL: str = "fed-media-v1"

#: Frame-type tags. Additive: a new tag is forward-compatible because
#: receivers skip unknown types (see :func:`iter_complete_frames`).
FRAME_TYPE_MEDIA_CHUNK: int = 1

#: Symmetric AEAD suite for the encrypted chunk payload. Carried inside
#: the signed+encrypted envelope metadata (never in cleartext) and
#: validated against :data:`SUPPORTED_MEDIA_AEAD_SUITES` on receipt —
#: per the CLAUDE.md crypto-suite rule, unknown suites are rejected with
#: no default fallback. AES-256 is Grover-resistant (~128-bit PQ); the
#: PQ lever for media is the key-delivery channel, not this primitive.
MEDIA_AEAD_SUITE_AESGCM_256: str = "aesgcm-256"
SUPPORTED_MEDIA_AEAD_SUITES: frozenset[str] = frozenset(
    {MEDIA_AEAD_SUITE_AESGCM_256},
)

#: Frame-format suite: how a media chunk travels as one binary frame —
#: the layout above, the signed envelope as the header, the AEAD-sealed
#: chunk as the payload, bound by the ``chunk_sha256`` inside the signed,
#: encrypted metadata. Carried as ``media_frame_suite`` in that signed
#: metadata on both transports and, on HTTPS, also as the body's suite byte
#: (:data:`MEDIA_FRAME_SUITE_IDS`) so the receiver knows the layout before
#: it parses; the two must agree. A receiver refuses a value outside
#: :data:`SUPPORTED_MEDIA_FRAME_SUITES` with
#: :class:`UnsupportedMediaFrameSuite`. Required over HTTPS; a DataChannel
#: frame from a sender that predates the field (v_14..v_80) defaults to
#: this single value.
MEDIA_FRAME_SUITE_V1: str = "fed-media-v1"
SUPPORTED_MEDIA_FRAME_SUITES: frozenset[str] = frozenset({MEDIA_FRAME_SUITE_V1})

#: The first bytes of a binary media body on the HTTPS inbox. ``\x00`` is
#: never valid at the start of a JSON text, so the inbox tells the two
#: apart from the body alone.
MEDIA_BODY_MAGIC: bytes = b"\x00SHM"

#: The suite byte after :data:`MEDIA_BODY_MAGIC` → the frame suite it names.
#: A new layout takes a new byte; an unknown byte is refused.
MEDIA_FRAME_SUITE_IDS: dict[int, str] = {0x01: MEDIA_FRAME_SUITE_V1}
_SUITE_ID_BY_NAME: dict[str, int] = {v: k for k, v in MEDIA_FRAME_SUITE_IDS.items()}
_BODY_PREFIX_LEN: int = len(MEDIA_BODY_MAGIC) + 1

#: The largest binary media body the inbox accepts: one
#: :data:`~socialhome.domain.media_chunking.HTTPS_CHUNK_BYTES` chunk plus
#: room for the magic, the signed header and the frame (528 KiB). Refused
#: from ``Content-Length`` once the magic is read, and while streaming.
HTTPS_MEDIA_MAX_BODY_BYTES: int = HTTPS_CHUNK_BYTES + FRAME_OVERHEAD_BYTES

#: Upper bounds to cap allocation from a malicious/garbled peer. A
#: federation envelope is a few KiB; a media chunk is bounded by the
#: sender's ``domain.media_chunking.HTTPS_CHUNK_BYTES`` (512 KiB) plus GCM overhead. Keep
#: generous headroom but reject absurd declared lengths before
#: allocating.
MAX_HEADER_BYTES: int = 1 << 20  # 1 MiB
MAX_PAYLOAD_BYTES: int = 4 << 20  # 4 MiB

_PREFIX = struct.Struct(">BI")  # frame_type (u8) + header_len (u32 BE)
_U32 = struct.Struct(">I")


class MediaFramingError(ValueError):
    """Raised when wire bytes don't match the frame spec.

    Inherits :class:`ValueError` so it maps to a 422 at any HTTP boundary
    it leaks through, matching :class:`~socialhome.services.highlight_public_framing.FramingError`.
    """

    __slots__ = ()


class UnsupportedMediaAeadSuite(ValueError):
    """Raised when a media chunk declares an AEAD suite we don't support.

    Mirrors the ``Unsupported*Suite`` pattern used across the federation
    crypto surfaces (signatures, KEM, key delivery). Receivers reject —
    never fall back to a default.
    """

    __slots__ = ()


class UnsupportedMediaFrameSuite(ValueError):
    """Raised when a media frame names a ``media_frame_suite`` we don't
    support. Receivers reject — never fall back to a default."""

    __slots__ = ()


@dataclass(slots=True, frozen=True)
class MediaFrame:
    """One decoded wire frame."""

    frame_type: int
    header: bytes
    payload: bytes


# ─── Encode ──────────────────────────────────────────────────────────────


def encode(
    header: bytes,
    payload: bytes = b"",
    *,
    frame_type: int = FRAME_TYPE_MEDIA_CHUNK,
) -> bytes:
    """Encode one wire frame.

    ``header`` and ``payload`` are bytes-like; ``payload`` may be ``b""``
    for control frames. Raises :class:`MediaFramingError` if either
    exceeds its size ceiling.
    """
    if not 0 <= frame_type <= 0xFF:
        raise MediaFramingError(f"frame_type out of range: {frame_type}")
    h = bytes(header)
    p = bytes(payload)
    if len(h) > MAX_HEADER_BYTES:
        raise MediaFramingError(f"header too large: {len(h)} > {MAX_HEADER_BYTES}")
    if len(p) > MAX_PAYLOAD_BYTES:
        raise MediaFramingError(f"payload too large: {len(p)} > {MAX_PAYLOAD_BYTES}")
    return _PREFIX.pack(frame_type, len(h)) + h + _U32.pack(len(p)) + p


# ─── Decode ──────────────────────────────────────────────────────────────


def _frame_span(buf: bytes, off: int) -> tuple[int, int, int, int] | None:
    """Return ``(header_off, header_len, payload_off, payload_len)`` for the
    frame starting at ``off``, or ``None`` if ``buf`` doesn't yet hold a
    complete frame. Raises :class:`MediaFramingError` on a structurally
    invalid (oversized) declared length.
    """
    n = len(buf)
    if n - off < _PREFIX.size:
        return None
    _ftype, header_len = _PREFIX.unpack_from(buf, off)
    if header_len > MAX_HEADER_BYTES:
        raise MediaFramingError(f"declared header_len too large: {header_len}")
    header_off = off + _PREFIX.size
    plen_off = header_off + header_len
    if n < plen_off + _U32.size:
        return None
    (payload_len,) = _U32.unpack_from(buf, plen_off)
    if payload_len > MAX_PAYLOAD_BYTES:
        raise MediaFramingError(f"declared payload_len too large: {payload_len}")
    payload_off = plen_off + _U32.size
    if n < payload_off + payload_len:
        return None
    return header_off, header_len, payload_off, payload_len


def decode(buf: bytes) -> MediaFrame:
    """Decode exactly one frame from ``buf`` (which must hold ≥ one frame).

    Trailing bytes after the first frame are ignored — use
    :func:`iter_complete_frames` for streaming reassembly.
    """
    span = _frame_span(buf, 0)
    if span is None:
        raise MediaFramingError("buffer too short for a complete frame")
    header_off, header_len, payload_off, payload_len = span
    frame_type = buf[0]
    return MediaFrame(
        frame_type=frame_type,
        header=bytes(buf[header_off : header_off + header_len]),
        payload=bytes(buf[payload_off : payload_off + payload_len]),
    )


def is_media_body(buf: bytes) -> bool:
    """Whether an inbox body is a binary media frame (it starts with
    :data:`MEDIA_BODY_MAGIC`) rather than a JSON envelope."""
    return bytes(buf[: len(MEDIA_BODY_MAGIC)]) == MEDIA_BODY_MAGIC


def encode_body(
    header: bytes, payload: bytes, *, suite: str = MEDIA_FRAME_SUITE_V1
) -> bytes:
    """One HTTPS inbox media body: magic, suite byte, one chunk frame."""
    suite_id = _SUITE_ID_BY_NAME.get(suite)
    if suite_id is None:
        raise UnsupportedMediaFrameSuite(f"unknown media frame suite: {suite!r}")
    return MEDIA_BODY_MAGIC + bytes([suite_id]) + encode(header, payload)


def decode_body(buf: bytes) -> tuple[str, MediaFrame]:
    """Decode an HTTPS inbox media body → ``(frame suite, frame)``.

    Refuses a body without the magic or too short (:class:`MediaFramingError`),
    an unknown suite byte (:class:`UnsupportedMediaFrameSuite` — no default),
    and, unlike :func:`decode`, trailing bytes (a body is one frame, nothing
    may ride behind it) and any frame type but
    :data:`FRAME_TYPE_MEDIA_CHUNK`.
    """
    if not is_media_body(buf) or len(buf) < _BODY_PREFIX_LEN:
        raise MediaFramingError("not a media body")
    suite = MEDIA_FRAME_SUITE_IDS.get(buf[len(MEDIA_BODY_MAGIC)])
    if suite is None:
        raise UnsupportedMediaFrameSuite(
            f"unknown media frame suite id: {buf[len(MEDIA_BODY_MAGIC)]}"
        )
    # Parse in place at the prefix offset: the header and the sealed chunk
    # are each copied out once, the body itself never (it is up to 528 KiB).
    span = _frame_span(buf, _BODY_PREFIX_LEN)
    if span is None:
        raise MediaFramingError("body too short for a complete frame")
    header_off, header_len, payload_off, payload_len = span
    if payload_off + payload_len != len(buf):
        raise MediaFramingError("trailing bytes after the frame")
    frame_type = buf[_BODY_PREFIX_LEN]
    if frame_type != FRAME_TYPE_MEDIA_CHUNK:
        raise MediaFramingError(f"unexpected frame_type: {frame_type}")
    view = memoryview(buf)
    return suite, MediaFrame(
        frame_type=frame_type,
        header=bytes(view[header_off : header_off + header_len]),
        payload=bytes(view[payload_off : payload_off + payload_len]),
    )


def iter_complete_frames(buf: bytes) -> tuple[list[MediaFrame], bytes]:
    """Extract every complete frame from ``buf``.

    Returns ``(frames, leftover)`` where ``leftover`` is the unconsumed
    tail (a partially-received frame) the caller should prepend to the
    next read. Raises :class:`MediaFramingError` only on a structurally
    invalid declared length — a merely-incomplete tail is returned as
    ``leftover``, not an error.
    """
    frames: list[MediaFrame] = []
    off = 0
    while off < len(buf):
        span = _frame_span(buf, off)
        if span is None:
            break
        header_off, header_len, payload_off, payload_len = span
        frames.append(
            MediaFrame(
                frame_type=buf[off],
                header=bytes(buf[header_off : header_off + header_len]),
                payload=bytes(buf[payload_off : payload_off + payload_len]),
            )
        )
        off = payload_off + payload_len
    return frames, bytes(buf[off:])
