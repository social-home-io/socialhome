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
from collections.abc import Iterable

import orjson

from .errors import SpaceItemTooLargeError

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
# payload as the frame's bytes. The same item must also fit the §25.6 sync
# chunk that heals a lost frame (the GFS keeps a frame one hour, a JSON
# envelope a day). A framed ``SPACE_SYNC_CHUNK`` carries its record's
# ciphertext RAW (``federation/relay_event_body.py`` — no base64), so a
# chunk costs only its wrapper on top of the record. The bound of a live
# event is DERIVED from the frame's: whatever travels live also fits a sync
# chunk.

#: Bytes a framed event needs beside its payload: the length field, the
#: media-body magic and framing, the signed header (a hybrid signature
#: included) and the AEAD nonce and tag.
RELAY_EVENT_FRAME_ALLOWANCE_BYTES: int = 8 * 1024

#: The largest payload JSON one frame carries — what a ``SPACE_SYNC_CHUNK``
#: event may be.
RELAY_EVENT_FRAME_MAX_PAYLOAD_BYTES: int = (
    RELAY_MEDIA_MAX_PLAINTEXT_BYTES - RELAY_EVENT_FRAME_ALLOWANCE_BYTES
)

#: What a framed sync chunk adds around its one record: the chunk
#: envelope's fields and signatures (a hybrid set included), the
#: ``records`` wrapper, the AEAD nonce and tag, the raw layout's meta, and
#: the fields a record has that the live event lacks.
SYNC_CHUNK_WRAP_ALLOWANCE_BYTES: int = 16 * 1024

#: The largest payload JSON of any OTHER framed event (a page, a pending
#: item, a config change): it fits a sync chunk's record, so the catch-up
#: can always re-deliver what went live. 515,998 bytes (~503 KiB).
RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES: int = (
    RELAY_EVENT_FRAME_MAX_PAYLOAD_BYTES - SYNC_CHUNK_WRAP_ALLOWANCE_BYTES
)

#: The largest §25.6 record (as JSON) a stream over the GFS relay carries:
#: with the chunk's wrapper it fills the frame. The same number as the live
#: bound, by construction.
RELAY_SYNC_RECORD_MAX_BYTES: int = RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES

# ── How large a space item's text may be: one limit per kind, everywhere ──
#
# Owner decision 2026-11-14: "Size should not matter if GFS or federation.
# In the end members are equal." So these limits hold in EVERY space and on
# every path — not only where a household is reached through a GFS — sized
# so that the worst-case event and the worst-case sync record of the item
# fit one relay frame. The text is not all that rides there: each limit is
# the bound less the HARD CAP of every list and field that can sit next to
# that kind of item, so nothing that passed can become undeliverable when
# a list grows later (``tests/federation/test_space_text_limits.py`` builds
# each worst case with the real builders).

#: Next to a page's text in its record and event: the files' descriptors
#: (``MAX_PAGE_DESCRIPTOR_ENTRIES`` = 64, ~330 bytes each with their hash),
#: the wrapper of up to three conflict sides, the cover, ids and stamps.
SPACE_PAGE_RESERVE_BYTES: int = 40 * 1024

#: A page's STATE — its title and content plus the title and content of
#: every open conflict side (a side is a whole other version, and its text
#: is not capped on its own): what the host keeps and every member mirrors.
#: 475,038 bytes (~463 KiB).
SPACE_PAGE_STATE_MAX_BYTES: int = (
    RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES - SPACE_PAGE_RESERVE_BYTES
)

#: Next to a calendar event's text in its record: up to
#: ``MAX_RSVPS_PER_EVENT`` (500) RSVPs (~200 bytes each), the cover's file
#: descriptors, the recurrence rule, ids and stamps.
SPACE_CALENDAR_RESERVE_BYTES: int = 128 * 1024

#: A space calendar event's summary + description + attendee ids (the one
#: list on the record with no cap of its own, so it is measured, not
#: reserved). 384,926 bytes (~375 KiB).
SPACE_CALENDAR_TEXT_MAX_BYTES: int = (
    RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES - SPACE_CALENDAR_RESERVE_BYTES
)

#: Next to a space's description / about text in ``SPACE_CONFIG_CHANGED``:
#: the relay variant's cover and icon (64 + 16 KiB, base64'ed), the feature
#: set and the other settings.
SPACE_CONFIG_RESERVE_BYTES: int = 128 * 1024

#: A space's description (counted TWICE: ``SPACE_CONFIG_CHANGED`` carries
#: it in the flat legacy shape and again inside ``space_meta``) + its about
#: text. 384,926 bytes (~375 KiB) — a description alone up to ~187 KiB.
SPACE_CONFIG_TEXT_MAX_BYTES: int = (
    RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES - SPACE_CONFIG_RESERVE_BYTES
)


def calendar_texts(
    summary: str | None, description: str | None, attendees: Iterable[str] = ()
) -> tuple[str | None, ...]:
    """What of a space calendar event counts against
    :data:`SPACE_CALENDAR_TEXT_MAX_BYTES`."""
    return (summary, description, *attendees)


def config_texts(
    description: str | None, about_markdown: str | None
) -> tuple[str | None, ...]:
    """What of a space's config counts against
    :data:`SPACE_CONFIG_TEXT_MAX_BYTES`: the description twice (the event
    carries it in both of its shapes), the about text once."""
    return (description, description, about_markdown)


def space_text_bytes(*texts: str | None) -> int:
    """The bytes *texts* take inside an event payload or a sync record: each
    as a JSON string (escapes count)."""
    return sum(len(orjson.dumps(t)) for t in texts if t)


def require_space_text_fits(
    *texts: str | None, limit: int, was: tuple[str | None, ...] = ()
) -> None:
    """Refuse (:class:`~socialhome.domain.errors.SpaceItemTooLargeError`,
    413) space text over *limit* — the limit of its kind of item, the same
    in every space. *was*: the text the item holds now; a save that makes
    an item stored before the limit no larger is let through, so such an
    item stays readable and can always be edited down."""
    size = space_text_bytes(*texts)
    if size <= limit or (was and size <= space_text_bytes(*was)):
        return
    raise SpaceItemTooLargeError(limit)


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
    "RELAY_SYNC_RECORD_MAX_BYTES",
    "SPACE_CALENDAR_RESERVE_BYTES",
    "SPACE_CALENDAR_TEXT_MAX_BYTES",
    "SPACE_CONFIG_RESERVE_BYTES",
    "SPACE_CONFIG_TEXT_MAX_BYTES",
    "SPACE_PAGE_RESERVE_BYTES",
    "SPACE_PAGE_STATE_MAX_BYTES",
    "SUPPORTED_RELAY_MEDIA_VERSIONS",
    "SYNC_CHUNK_WRAP_ALLOWANCE_BYTES",
    "VALID_SEALED_LENGTHS",
    "InvalidRelayMediaBody",
    "build_relay_media_body",
    "calendar_texts",
    "config_texts",
    "is_relay_media_body",
    "parse_relay_media_body",
    "require_space_text_fits",
    "space_text_bytes",
]
