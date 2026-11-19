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
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import orjson

from .calendar import MAX_RSVPS_PER_EVENT, RSVP_RECORD_MAX_BYTES
from .errors import SpaceItemTooLargeError
from .federation import FederationEventType
from .gfs_server import MAX_GFS_SERVERS_JSON_BYTES
from .media_constraints import (
    SPACE_COVER_MAX_BYTES,
    SPACE_ICON_MAX_BYTES,
)

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

#: Framed events whose bytes may fill the frame: a §25.6 chunk (its record
#: is bounded by :data:`RELAY_SYNC_RECORD_MAX_BYTES`, its wrapper fills the
#: rest) and a ``SPACE_ROUTED`` wrapper (the host hop's leg — its sealed
#: inner, raw in the frame, is a live event or a sync chunk already bounded
#: where it was sent; the wrapper adds only the route's fields).
_FRAME_FILLING_EVENT_TYPES: frozenset[FederationEventType] = frozenset(
    {FederationEventType.SPACE_SYNC_CHUNK, FederationEventType.SPACE_ROUTED}
)


def relay_event_payload_limit(event_type: FederationEventType) -> int:
    """The most bytes a framed *event_type* payload may be: the frame's for
    a sync chunk and a routed wrapper, the live bound for anything else (so
    it also fits the sync chunk that heals a lost frame)."""
    if event_type in _FRAME_FILLING_EVENT_TYPES:
        return RELAY_EVENT_FRAME_MAX_PAYLOAD_BYTES
    return RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES


# ── How large a space item may be: one limit per kind, everywhere ───────
#
# Owner decision 2026-11-14: "Size should not matter if GFS or federation.
# In the end members are equal." So these limits hold in EVERY space and on
# every path — not only where a household is reached through a GFS — sized
# so that the worst-case event and the worst-case sync record of the item
# fit one relay frame.
#
# An item is measured WHOLE: its record exactly as it travels (orjson, as
# every payload is written), every field — so no field, today's or a future
# one, can grow it unseen. Held out is only what OTHER people add after it
# was saved, each under its own hard cap, and reserved next to the limit
# (``tests/federation/test_space_item_worst_case.py`` builds each worst case
# with the real builders).

#: Next to a page's record in its event and its sync record: the files'
#: descriptors and hashes the provider adds per requester
#: (``MAX_PAGE_DESCRIPTOR_ENTRIES`` = 64, ~450 bytes each) and the few
#: fields the live event has that the record lacks.
SPACE_PAGE_RESERVE_BYTES: int = 40 * 1024

#: A page: its §25.6 record (``domain.page_version.page_record``) — title,
#: content, cover, ids, stamps AND every open conflict side (a side is a
#: whole other version, added only by the host, which refuses one that
#: would pass this). 475,038 bytes (~475 kB).
SPACE_PAGE_ITEM_MAX_BYTES: int = (
    RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES - SPACE_PAGE_RESERVE_BYTES
)

#: Next to a calendar event's record: up to ``MAX_RSVPS_PER_EVENT`` RSVPs
#: of at most ``RSVP_RECORD_MAX_BYTES`` each (added by other people after
#: the event was saved), plus 4 KiB for the RSVP fields, the cover's file
#: descriptor and the few fields the live event has that the record lacks.
SPACE_CALENDAR_RESERVE_BYTES: int = (
    MAX_RSVPS_PER_EVENT * (RSVP_RECORD_MAX_BYTES + 1) + 4 * 1024
)

#: A space calendar event: its §25.6 record
#: (``domain.calendar.calendar_record``) — summary, description, attendee
#: ids, rule, location, cover, ids, stamps. 383,402 bytes (~383 kB).
SPACE_CALENDAR_ITEM_MAX_BYTES: int = (
    RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES - SPACE_CALENDAR_RESERVE_BYTES
)


def _base64_len(n: int) -> int:
    return 4 * -(-n // 3)


#: Next to a space's config in ``SPACE_CONFIG_CHANGED``: the cover and icon
#: at the one cap every member gets (base64'ed: 87,384 + 21,848 bytes), the owner's signed GFS list at
#: its cap, and 16 KiB for the authority cert, the owner's statement, the
#: signatures (a hybrid set included) and the event's own fields.
SPACE_CONFIG_RESERVE_BYTES: int = (
    _base64_len(SPACE_COVER_MAX_BYTES)
    + _base64_len(SPACE_ICON_MAX_BYTES)
    + MAX_GFS_SERVERS_JSON_BYTES
    + 16 * 1024
)

#: A space's config: its plain config fields as they travel
#: (``services.space_service.space_metadata_fields`` — name, emoji,
#: description, about text, features, every setting). The event carries it
#: TWICE (the flat legacy shape and ``space_meta``), so the limit is half
#: of what is left. 178,807 bytes (~178 kB).
SPACE_CONFIG_ITEM_MAX_BYTES: int = (
    RELAY_EVENT_MAX_LIVE_PAYLOAD_BYTES - SPACE_CONFIG_RESERVE_BYTES
) // 2


def space_item_bytes(item: Mapping[str, Any]) -> int:
    """The bytes *item* (a space item's record) takes on the wire: its JSON
    exactly as a payload is written — every field, separators, escapes."""
    return len(orjson.dumps(item))


def space_item_too_large(
    item: Mapping[str, Any], *, limit: int, was: Mapping[str, Any] | None = None
) -> bool:
    """Whether *item* is over *limit* — the limit of its kind, the same in
    every space. Strict, with one exception: when *was* (the item as held
    now) is ITSELF over the limit — stored before the limit existed — a
    version no larger than it is not too large, so such an item stays
    readable and can be edited down. Both are measured the same way (the
    kind's measure, stamps at their widest)."""
    size = space_item_bytes(item)
    if size <= limit:
        return False
    if was is None:
        return True
    held = space_item_bytes(was)
    return held <= limit or size > held


# ── Stamps, ids and counters: bounded where they are read ─────────────────
#
# A record's who / when / which-version fields are not what a person
# writes: they differ between the household that saves an item, the host
# that sequences it and every household that receives it. Each kind's
# measure counts them at their WIDEST (so all of them get the same number
# for the same content), and every path that reads them from the wire
# bounds them to that width — so the record that travels is never larger
# than the one measured.

#: The longest timestamp kept (ISO 8601, microseconds and an offset fit).
STAMP_MAX_CHARS: int = 64

#: The longest id kept (a user, an item, a household, a key) — the
#: roster's own user id cap.
ITEM_ID_MAX_CHARS: int = 128

#: The largest counter (a sequence number) kept: a signed 64-bit integer,
#: what SQLite stores and orjson writes.
COUNTER_MAX: int = 2**63 - 1

#: Each at its widest, for the measure.
WIDEST_STAMP: str = "9" * STAMP_MAX_CHARS
WIDEST_ID: str = "9" * ITEM_ID_MAX_CHARS


def bounded_stamp(value: object) -> str:
    """A timestamp read from the wire: an ISO 8601 string of at most
    :data:`STAMP_MAX_CHARS` that parses; anything else is ``""`` (none)."""
    if not isinstance(value, str) or not value or len(value) > STAMP_MAX_CHARS:
        return ""
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return ""
    return value


def bounded_id(value: object) -> str:
    """An id read from the wire: a string of at most
    :data:`ITEM_ID_MAX_CHARS`; anything else is ``""`` (none)."""
    if not isinstance(value, str) or len(value) > ITEM_ID_MAX_CHARS:
        return ""
    return value


def bounded_counter(value: object, default: int = 0) -> int:
    """A counter read from the wire: an integer (never a bool or a float)
    in ``0..COUNTER_MAX``; anything else is *default*."""
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if 0 <= value <= COUNTER_MAX else default


def require_space_item_fits(
    item: Mapping[str, Any], *, limit: int, was: Mapping[str, Any] | None = None
) -> None:
    """Refuse (:class:`~socialhome.domain.errors.SpaceItemTooLargeError`,
    413) an item :func:`space_item_too_large` says is too large."""
    if space_item_too_large(item, limit=limit, was=was):
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
    "COUNTER_MAX",
    "ENVELOPE_MEDIA_CAPABILITY",
    "ITEM_ID_MAX_CHARS",
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
    "SPACE_CALENDAR_ITEM_MAX_BYTES",
    "SPACE_CALENDAR_RESERVE_BYTES",
    "SPACE_CONFIG_ITEM_MAX_BYTES",
    "SPACE_CONFIG_RESERVE_BYTES",
    "SPACE_PAGE_ITEM_MAX_BYTES",
    "SPACE_PAGE_RESERVE_BYTES",
    "STAMP_MAX_CHARS",
    "SUPPORTED_RELAY_MEDIA_VERSIONS",
    "SYNC_CHUNK_WRAP_ALLOWANCE_BYTES",
    "VALID_SEALED_LENGTHS",
    "WIDEST_ID",
    "WIDEST_STAMP",
    "InvalidRelayMediaBody",
    "bounded_counter",
    "bounded_id",
    "bounded_stamp",
    "build_relay_media_body",
    "is_relay_media_body",
    "parse_relay_media_body",
    "require_space_item_fits",
    "space_item_bytes",
    "space_item_too_large",
]
