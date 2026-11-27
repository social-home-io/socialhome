"""What a household's GFS socket takes — the ``accepts`` labels of its hello.

The labels are an unsigned, per-connection hint next to the signed WS hello
(and the mailbox claim socket's): the GFS checks the shape and gates optional
deliveries on them so an older household is never sent what it would drop. A
GFS could add or strip labels; it is a hint, not an authorization. A
malformed label is dropped, never an error.

Since v_96 the household takes no space-keyed seat, so the seat labels of
the space-keyed relay (``space_key_pull``, ``space_item``, ``payload_zlib1``,
``payload_zlib1_dict1``) went with it; only the per-connection labels stay.

Pure, no I/O.
"""

from __future__ import annotations

import re

#: The household reads binary GFS→household WebSocket frames
#: (:mod:`socialhome.domain.ws_binary_frame`).
ACCEPTS_WS_BINARY1: str = "ws_binary1"

#: The household answers a GFS's ``space_media_request`` (v_83): it
#: re-uploads or streams a space blob a reader missed, when it holds the
#: file. The GFS asks only sockets that listed it (an older household would
#: drop the unknown frame and leave a reader waiting).
ACCEPTS_SPACE_MEDIA_SERVE: str = "space_media_serve"

#: The household opens binary relay media frames (v_87, ``\x00SHR`` —
#: :mod:`socialhome.domain.relay_media_frame`) pushed as BINARY WebSocket
#: messages. The GFS pushes a queued media frame only to a socket that
#: listed it (an older household would drop the unknown binary message after
#: the GFS dequeued it).
ACCEPTS_ENVELOPE_MEDIA: str = "envelope_media"

#: v_97 — the household reads its channels from the GFS's channel ring: the
#: socket sends ``channel_watch`` / ``channel_fetch`` frames, gets live items
#: only for the channels it watches, a
#: ``channel_heads`` ping, and no offline copies of channel items
#: (``docs/protocol/space-items.md``).
ACCEPTS_CHANNEL_RING: str = "channel_ring"

#: What this household lists in its WS hello.
OUR_HELLO_ACCEPTS: tuple[str, ...] = (
    ACCEPTS_WS_BINARY1,
    ACCEPTS_SPACE_MEDIA_SERVE,
    ACCEPTS_ENVELOPE_MEDIA,
    ACCEPTS_CHANNEL_RING,
)

#: The most labels one hello may carry.
MAX_ACCEPTS_LABELS: int = 16

#: The longest one label may be.
MAX_ACCEPTS_LABEL_CHARS: int = 48

_LABEL_RE = re.compile(r"^[a-z0-9_]+$")


def parse_accepts(raw: object) -> tuple[str, ...]:
    """The usable labels of a wire ``accepts`` value — never raises.

    It is an unsigned hint, so a malformed value never fails the hello:
    anything but a list counts as ``()``, a label that is not a short
    ``[a-z0-9_]`` string is dropped, duplicates collapse in order, and at
    most :data:`MAX_ACCEPTS_LABELS` are kept. Unknown well-formed labels
    are kept (the list is extensible)."""
    if not isinstance(raw, list):
        return ()
    labels: list[str] = []
    for label in raw:
        if len(labels) >= MAX_ACCEPTS_LABELS:
            break
        if (
            isinstance(label, str)
            and len(label) <= MAX_ACCEPTS_LABEL_CHARS
            and _LABEL_RE.match(label)
            and label not in labels
        ):
            labels.append(label)
    return tuple(labels)
