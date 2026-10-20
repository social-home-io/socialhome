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
host's per-space rate backstop bounds what that costs.

Pure, no I/O.
"""

from __future__ import annotations

import re

#: The follower opens the sealed ``space_author_profiles_bundle`` (v_61).
ACCEPTS_AUTHOR_PROFILES_BUNDLE: str = "author_profiles_bundle"

#: What this household lists on every subscribe — follower and member seats
#: alike, so the label never tells the GFS which seats are writers'.
OUR_ACCEPTS: tuple[str, ...] = (ACCEPTS_AUTHOR_PROFILES_BUNDLE,)

#: The most labels one subscribe may carry.
MAX_ACCEPTS_LABELS: int = 16

#: The longest one label may be.
MAX_ACCEPTS_LABEL_CHARS: int = 48

_LABEL_RE = re.compile(r"^[a-z0-9_]+$")


def parse_accepts(raw: object) -> tuple[str, ...]:
    """Validate a wire ``accepts`` value. ``None`` (absent) is ``()``.

    Unknown labels are kept (the list is extensible) and de-duplicated in
    order. Raises :class:`ValueError` on anything but a bounded list of
    short ``[a-z0-9_]`` strings."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > MAX_ACCEPTS_LABELS:
        raise ValueError(f"accepts must be a list of at most {MAX_ACCEPTS_LABELS}")
    labels: list[str] = []
    for label in raw:
        if (
            not isinstance(label, str)
            or len(label) > MAX_ACCEPTS_LABEL_CHARS
            or not _LABEL_RE.match(label)
        ):
            raise ValueError("malformed accepts label")
        if label not in labels:
            labels.append(label)
    return tuple(labels)
