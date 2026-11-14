"""The outer shape of a binary media frame on the GFS envelope relay.

A household reached only through a GFS (a member seated from an invite link)
used to be unreachable for media: the relay took JSON envelopes up to
~191 KiB, so a 512 KiB chunk (1 MiB once base64'ed twice) never fit. From
v_87 a household seals the very ``fed-media-v1`` body it would POST to a
peer's inbox (``\\x00SHM`` …, :mod:`socialhome.federation.media_framing`) to
the recipient's key-wrap key and hands the GFS the raw bytes::

    [RELAY_MEDIA_MAGIC "\\x00SHR"][u8 version = 1][to_instance: 32 ASCII]
    [sealed: opaque to the GFS]

``POST /gfs/envelope`` tells it from a JSON envelope by the body alone
(``\\x00`` can never start a JSON text), exactly like the household inbox
tells ``\\x00SHM`` from JSON. The GFS reads the magic, the version and the
recipient id — the routing it already reads from a JSON envelope — and
nothing else: no sender, no event type, no space. It queues and pushes the
body **unchanged** to the recipient's ``/gfs/ws`` as one BINARY WebSocket
message. The recipient tells it from a ``ws_binary1`` frame by the first byte
(``ws_binary1`` starts with its version byte ``1``, this with ``0``).

What is inside ``sealed`` is the household's business
(:mod:`socialhome.federation.gfs_relay_media`); this module is the one place
both halves agree on the outer bytes, so it is pure and imports nothing from
either.
"""

from __future__ import annotations

import re

#: First bytes of a binary relay media body (household → GFS → household).
RELAY_MEDIA_MAGIC: bytes = b"\x00SHR"

#: The one outer layout this build writes and reads. A new layout takes a new
#: byte; an unknown one is refused (by the GFS with a 400, by the household
#: by dropping the frame).
RELAY_MEDIA_VERSION_1: int = 1
SUPPORTED_RELAY_MEDIA_VERSIONS: frozenset[int] = frozenset({RELAY_MEDIA_VERSION_1})

#: The recipient id is an instance id: 32 characters of lowercase base32.
RELAY_MEDIA_TO_CHARS: int = 32
_INSTANCE_ID_RE = re.compile(rb"[a-z2-7]{32}")

#: Magic + version + recipient id.
RELAY_MEDIA_PREFIX_BYTES: int = len(RELAY_MEDIA_MAGIC) + 1 + RELAY_MEDIA_TO_CHARS

#: The largest binary relay media body (what the GFS accepts and the
#: household sends): one 512 KiB chunk's media body sealed and padded, 528 KiB
#: — the household inbox's own media body cap. Media frames only; a JSON
#: envelope keeps its 320 KiB cap.
RELAY_MEDIA_MAX_BODY_BYTES: int = 528 * 1024

#: Bytes the household's seal adds around the padded plaintext: kem-suite
#: byte, ephemeral X25519 key, AES-GCM nonce and tag
#: (:mod:`socialhome.federation.gfs_relay_media`).
RELAY_MEDIA_SEAL_OVERHEAD_BYTES: int = 1 + 32 + 12 + 16

#: The largest padded plaintext an outer body can carry.
RELAY_MEDIA_MAX_PLAINTEXT_BYTES: int = (
    RELAY_MEDIA_MAX_BODY_BYTES
    - RELAY_MEDIA_PREFIX_BYTES
    - RELAY_MEDIA_SEAL_OVERHEAD_BYTES
)

#: Padded-plaintext sizes a frame is padded up to, so the GFS sees a bucket,
#: not a size. A full 512 KiB chunk's media body (~514 KiB) lands in 516 KiB;
#: the top rung is the cap itself.
RELAY_MEDIA_SIZE_BUCKETS: tuple[int, ...] = (
    64 * 1024,
    128 * 1024,
    256 * 1024,
    516 * 1024,
    RELAY_MEDIA_MAX_PLAINTEXT_BYTES,
)

#: The only ``sealed`` lengths a body may have: one bucket plus the seal
#: overhead. Both the GFS and the household refuse any other length, so no
#: off-bucket size ever crosses the relay.
VALID_SEALED_LENGTHS: frozenset[int] = frozenset(
    b + RELAY_MEDIA_SEAL_OVERHEAD_BYTES for b in RELAY_MEDIA_SIZE_BUCKETS
)

# ── A space event in a frame: one bound, live and by sync ────────────────
#
# A space event too large for a JSON relay envelope rides one frame, its
# payload JSON as the frame's bytes. The same item must also fit the §25.6
# sync chunk that heals a lost frame (the GFS keeps a frame one hour, a JSON
# envelope a day), and a chunk carries its record encrypted and base64'ed
# (x 4/3) inside the ``SPACE_SYNC_CHUNK`` event that rides the very same
# frame. So the bound of a live event is DERIVED from the frame's, and
# whatever travels live also fits a sync chunk.

#: Bytes a framed event needs beside its payload: the length field, the
#: media-body magic and framing, the signed header (a hybrid signature
#: included) and the AEAD nonce and tag.
RELAY_EVENT_FRAME_ALLOWANCE_BYTES: int = 8 * 1024

#: The largest payload JSON one frame carries — what a ``SPACE_SYNC_CHUNK``
#: event may be.
RELAY_EVENT_FRAME_MAX_PAYLOAD_BYTES: int = (
    RELAY_MEDIA_MAX_PLAINTEXT_BYTES - RELAY_EVENT_FRAME_ALLOWANCE_BYTES
)

#: What a sync chunk adds around its one record besides the x 4/3: the
#: chunk envelope's fields and signatures, the ``records`` wrapper, the
#: AEAD nonce and tag, and the fields a record has that the live event
#: lacks.
SYNC_CHUNK_WRAP_ALLOWANCE_BYTES: int = 16 * 1024

#: The largest payload JSON of any OTHER framed event (a page, a pending
#: item, a config change): it fits a sync chunk's record, so the catch-up
#: can always re-deliver what went live. ~378 KiB.
RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES: int = (
    (RELAY_EVENT_FRAME_MAX_PAYLOAD_BYTES - SYNC_CHUNK_WRAP_ALLOWANCE_BYTES) * 3 // 4
)

#: The largest text (as JSON) a person may save into a space that has a
#: household reached only through a GFS — a page's title and content, a
#: description: the live bound less room for the event's other fields.
SPACE_TEXT_MAX_BYTES_BEHIND_GFS: int = RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES - 8 * 1024

#: The GFS capability (signed ``/gfs/info`` block) that says it carries these
#: bodies, and the hello ``accepts`` label a household lists so the GFS
#: pushes them to its socket (an older household's socket would drop them).
ENVELOPE_MEDIA_CAPABILITY: str = "envelope_media"


class InvalidRelayMediaBody(ValueError):
    """A body that is not a well-formed binary relay media body."""


def is_relay_media_body(buf: bytes) -> bool:
    """Whether *buf* starts with :data:`RELAY_MEDIA_MAGIC`."""
    return bytes(buf[: len(RELAY_MEDIA_MAGIC)]) == RELAY_MEDIA_MAGIC


def build_relay_media_body(to_instance: str, sealed: bytes) -> bytes:
    """The outer body for *sealed* addressed to *to_instance*."""
    raw_to = to_instance.encode("ascii")
    if not _INSTANCE_ID_RE.fullmatch(raw_to):
        raise InvalidRelayMediaBody("invalid recipient id")
    return RELAY_MEDIA_MAGIC + bytes([RELAY_MEDIA_VERSION_1]) + raw_to + sealed


def parse_relay_media_body(buf: bytes) -> tuple[str, memoryview]:
    """``(to_instance, sealed)`` of an outer body, or
    :class:`InvalidRelayMediaBody`: no magic, an unknown version, a
    recipient that is not an instance id, a ``sealed`` length that is not one
    of :data:`VALID_SEALED_LENGTHS`, or over :data:`RELAY_MEDIA_MAX_BODY_BYTES`. ``sealed`` is a view into *buf*
    (nothing is copied)."""
    if not is_relay_media_body(buf):
        raise InvalidRelayMediaBody("not a relay media body")
    if len(buf) > RELAY_MEDIA_MAX_BODY_BYTES:
        raise InvalidRelayMediaBody("relay media body too large")
    if len(buf) <= RELAY_MEDIA_PREFIX_BYTES:
        raise InvalidRelayMediaBody("relay media body too short")
    version = buf[len(RELAY_MEDIA_MAGIC)]
    if version not in SUPPORTED_RELAY_MEDIA_VERSIONS:
        raise InvalidRelayMediaBody(f"unknown relay media version {version}")
    to_raw = bytes(buf[len(RELAY_MEDIA_MAGIC) + 1 : RELAY_MEDIA_PREFIX_BYTES])
    if not _INSTANCE_ID_RE.fullmatch(to_raw):
        raise InvalidRelayMediaBody("invalid recipient id")
    if len(buf) - RELAY_MEDIA_PREFIX_BYTES not in VALID_SEALED_LENGTHS:
        raise InvalidRelayMediaBody("invalid sealed length")
    return to_raw.decode("ascii"), memoryview(buf)[RELAY_MEDIA_PREFIX_BYTES:]


__all__ = [
    "ENVELOPE_MEDIA_CAPABILITY",
    "RELAY_EVENT_FRAME_ALLOWANCE_BYTES",
    "RELAY_EVENT_FRAME_MAX_PAYLOAD_BYTES",
    "RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES",
    "RELAY_MEDIA_MAGIC",
    "RELAY_MEDIA_MAX_BODY_BYTES",
    "RELAY_MEDIA_MAX_PLAINTEXT_BYTES",
    "RELAY_MEDIA_PREFIX_BYTES",
    "RELAY_MEDIA_SEAL_OVERHEAD_BYTES",
    "RELAY_MEDIA_SIZE_BUCKETS",
    "RELAY_MEDIA_TO_CHARS",
    "RELAY_MEDIA_VERSION_1",
    "SPACE_TEXT_MAX_BYTES_BEHIND_GFS",
    "SUPPORTED_RELAY_MEDIA_VERSIONS",
    "SYNC_CHUNK_WRAP_ALLOWANCE_BYTES",
    "VALID_SEALED_LENGTHS",
    "InvalidRelayMediaBody",
    "build_relay_media_body",
    "is_relay_media_body",
    "parse_relay_media_body",
]
