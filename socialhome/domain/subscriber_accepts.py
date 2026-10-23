"""What a GFS follower can open — the ``accepts`` labels of ``/gfs/subscribe``.

A household subscribing to a public / global space at a GFS lists the
optional relay deliveries it understands (``"accepts": [...]``, v_61). The
field rides NEXT to the signed ``{action, instance_id, space_id, ts}`` bytes,
not inside them, so an older GFS still verifies the subscribe; it is a hint,
not an authorization. The GFS checks the shape, stores nothing, and forwards
the labels in the ``new_subscriber`` frame it sends the space's host for a
NEW seat. The host sends a delivery only to a follower that lists it — so an
older follower, which would drop the unknown relay type, is never sent the
up-to-~1 MB author-profiles bundle. A GFS could add or strip labels; the
host's per-space and per-follower backstops bound what that costs. A
malformed label is dropped, never an error: it is a hint.

Pure, no I/O.
"""

from __future__ import annotations

import re

#: The follower opens the sealed ``space_author_profiles_bundle`` (v_61).
ACCEPTS_AUTHOR_PROFILES_BUNDLE: str = "author_profiles_bundle"

#: The follower asks for a space's content key when it misses one
#: (``POST /gfs/spaces/{id}/key_request``, #5b) — so the GFS need not have
#: the host re-seal it on every reconnect of the follower's socket (the
#: hello and each seat re-take). Also sent in the WS hello.
ACCEPTS_SPACE_KEY_PULL: str = "space_key_pull"

#: The household reads a member's own ``space_item`` publish (v_49), so a
#: seed holder's ``space_post_public`` copy of that post is redundant for it.
#: The GFS keeps this per seat (GFS migration 0023) and tells a seed holder
#: how many followers lack it (step 12).
ACCEPTS_SPACE_ITEM: str = "space_item"

#: What this household lists on every subscribe — follower and member seats
#: alike, so the label never tells the GFS which seats are writers'.
OUR_ACCEPTS: tuple[str, ...] = (
    ACCEPTS_AUTHOR_PROFILES_BUNDLE,
    ACCEPTS_SPACE_KEY_PULL,
    ACCEPTS_SPACE_ITEM,
)

#: The most labels one subscribe may carry.
MAX_ACCEPTS_LABELS: int = 16

#: The longest one label may be.
MAX_ACCEPTS_LABEL_CHARS: int = 48

_LABEL_RE = re.compile(r"^[a-z0-9_]+$")


def parse_accepts(raw: object) -> tuple[str, ...]:
    """The usable labels of a wire ``accepts`` value — never raises.

    It is an unsigned hint, so a malformed value never fails the subscribe:
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
