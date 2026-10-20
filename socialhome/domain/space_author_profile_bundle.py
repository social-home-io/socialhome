"""A new GFS follower's author-profile bundle (v_61) — pure, no I/O.

When a household follows a public / global space through a GFS, the space's
host answers the GFS ``new_subscriber`` notify with the content-key handoff
and then with ONE bundle of the space's author profiles, delivered to that
follower only (``POST /gfs/publish`` with ``to``). Before v_61 every member
household republished its authors' ``author_profile`` items to every
follower of the space instead.

The bundle carries the authors' own signed ``author_profile`` items
unchanged, so the follower verifies each author's signature and writer cert
exactly as for a relayed item; the host only chooses which and seals them
(:func:`~socialhome.federation.keywrap_seal.seal_to_keywrap`, KEM-suite
tagged) to the follower's key-wrap key. The plaintext is
``{"space_id", "epoch", "part", "parts", "items": [...]}``.

Bounded: at most :data:`MAX_BUNDLE_AUTHORS` authors (the most recently
stamped), :data:`MAX_BUNDLE_BYTES` of items in all, split into parts of at
most :data:`BUNDLE_PART_MAX_BYTES` so each sealed part fits the GFS's
publish body cap.
"""

from __future__ import annotations

import json

#: The most authors one new follower is sent (the most recently stamped).
MAX_BUNDLE_AUTHORS: int = 50

#: The most item bytes (compact JSON) one new follower is sent in all.
MAX_BUNDLE_BYTES: int = 1024 * 1024

#: The most item bytes in one sealed part: sealed and base64-encoded it stays
#: well under the GFS's 256 KiB ``/gfs/publish`` body cap.
BUNDLE_PART_MAX_BYTES: int = 160 * 1024


def _size(item: dict) -> int:
    return len(json.dumps(item, separators=(",", ":"), ensure_ascii=False).encode())


def split_bundle(items: list[dict]) -> list[list[dict]]:
    """The items as parts, in order, within every bound above. An item too
    big for a part on its own is left out."""
    parts: list[list[dict]] = []
    part: list[dict] = []
    part_bytes = 0
    total = 0
    for item in items[:MAX_BUNDLE_AUTHORS]:
        size = _size(item)
        if size > BUNDLE_PART_MAX_BYTES:
            continue
        if total + size > MAX_BUNDLE_BYTES:
            break
        if part and part_bytes + size > BUNDLE_PART_MAX_BYTES:
            parts.append(part)
            part, part_bytes = [], 0
        part.append(item)
        part_bytes += size
        total += size
    if part:
        parts.append(part)
    return parts


def bundle_plaintext(
    *, space_id: str, epoch: int, part: int, parts: int, items: list[dict]
) -> bytes:
    """One part's plaintext, before sealing."""
    return json.dumps(
        {
            "space_id": space_id,
            "epoch": epoch,
            "part": part,
            "parts": parts,
            "items": items,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def parse_bundle_plaintext(raw: bytes) -> tuple[str, int, list[object]] | None:
    """``(space_id, epoch, items)`` of an opened part, or ``None`` when it
    is not a bundle."""
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    space_id = body.get("space_id")
    epoch = body.get("epoch")
    items = body.get("items")
    if (
        not isinstance(space_id, str)
        or not space_id
        or not isinstance(epoch, int)
        or isinstance(epoch, bool)
        or epoch < 0
        or not isinstance(items, list)
        or len(items) > MAX_BUNDLE_AUTHORS
    ):
        return None
    return space_id, epoch, items
