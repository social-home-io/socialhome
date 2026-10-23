"""A new GFS follower's author-profile bundle (v_61). Pure, no I/O.

When a household follows a public or global space through a GFS, the
space's host answers the GFS ``new_subscriber`` notify for a NEW seat with
the content-key handoff, then with this bundle of the space's author
profiles. Both go to that follower only (``POST /gfs/publish`` with ``to``).

**Names first.** Part 0 carries every bundled author's own signed
``author_profile`` item, unchanged except that the unsigned picture
sidecar is left out. Each item is about 1–2 KB. The follower verifies each
author's signature, and their writer cert at the item's OWN epoch, exactly
as for a relayed item. So a bundle can hold profiles published before the
current epoch, and a key rotation never forces a republish. Later parts carry
only pictures: ``{author, hash, picture}``, where ``hash`` is the author's
SIGNED picture hash. The follower keeps a picture only when its bytes hash
to it. The host sends picture parts only while its hourly byte budget
allows, so names never wait behind pictures. The GFS delivers one
subscriber's frames in publish order, so part 0 lands first; a picture part
that somehow came first finds no row and is dropped.

Plaintext of one part::

    {"space_id", "part", "parts",
     "items": [{"epoch": int, "item": {...}}],
     "pictures": [{"author", "hash", "picture"}],
     "_pad": "000…"}

Every part is padded to a size bucket
(:data:`~socialhome.domain.space_item.ITEM_SIZE_BUCKETS`), so its length
tells the GFS only the bucket. The part COUNT still tells it roughly how
many bundled authors have a picture, and at most 50 authors are bundled.

Bounded: at most :data:`MAX_BUNDLE_AUTHORS` authors, the most recently
stamped first. Every part is at most :data:`BUNDLE_PART_MAX_BYTES` (the
largest bucket), so each one, sealed and base64'd, fits the GFS's 256 KiB
``/gfs/publish`` body cap.
"""

from __future__ import annotations

from dataclasses import dataclass

import orjson

from .space_author_profile import (
    PROFILE_PICTURE_FIELD,
    stored_profile_inner,
    without_picture_sidecar,
)
from .space_item import ITEM_SIZE_BUCKETS, pad_json_object

#: The most authors one new follower is sent (the most recently stamped).
MAX_BUNDLE_AUTHORS: int = 50

#: The most bytes of one part's plaintext, padding included: the largest
#: size bucket. Sealed and base64'd that stays under the GFS's 256 KiB cap.
BUNDLE_PART_MAX_BYTES: int = ITEM_SIZE_BUCKETS[-1]

#: Room kept for the part's own keys, numbering and padding field.
_PART_OVERHEAD: int = 256


@dataclass(slots=True, frozen=True)
class BundleEntry:
    """One author's cached, author-signed profile item, as the host holds it.

    ``item`` may still carry the picture sidecar (the author's original
    picture bytes). ``picture_hash`` is the hash the author signed."""

    epoch: int
    item: dict
    picture_hash: str | None


@dataclass(slots=True, frozen=True)
class ParsedBundlePart:
    """One opened part: ``items`` as ``(epoch, item)`` and ``pictures`` as
    ``(author_user_id, signed_hash, base64_webp)``. Nothing is verified yet."""

    space_id: str
    items: list[tuple[int, dict]]
    pictures: list[tuple[str, str, str]]


def _size(value: object) -> int:
    return len(orjson.dumps(value))


def build_bundle_parts(space_id: str, entries: list[BundleEntry]) -> list[bytes]:
    """The bundle's plaintext parts, in order, each padded to a bucket.

    Part 0 has the names (as many of the first :data:`MAX_BUNDLE_AUTHORS` as
    fit in one part). Then come the pictures of those authors, packed into
    as few parts as fit; a picture too big for a part on its own is left
    out. Returns ``[]`` when there is nothing to send."""
    names: list[dict] = []
    names_bytes = _PART_OVERHEAD
    pictures: list[dict] = []
    for entry in entries[:MAX_BUNDLE_AUTHORS]:
        item = stored_profile_inner(entry.item)
        if item is None:
            continue
        named = {"epoch": entry.epoch, "item": without_picture_sidecar(item)}
        size = _size(named) + 2
        if names_bytes + size > BUNDLE_PART_MAX_BYTES:
            continue
        names.append(named)
        names_bytes += size
        raw = item.get(PROFILE_PICTURE_FIELD)
        if entry.picture_hash and isinstance(raw, str) and raw:
            pictures.append(
                {
                    "author": str(entry.item.get("author_user_id") or ""),
                    "hash": entry.picture_hash,
                    "picture": raw,
                }
            )
    if not names:
        return []
    bodies: list[tuple[list[dict], list[dict]]] = [(names, [])]
    chunk: list[dict] = []
    chunk_bytes = _PART_OVERHEAD
    for picture in pictures:
        size = _size(picture) + 2
        if _PART_OVERHEAD + size > BUNDLE_PART_MAX_BYTES:
            continue
        if chunk and chunk_bytes + size > BUNDLE_PART_MAX_BYTES:
            bodies.append(([], chunk))
            chunk, chunk_bytes = [], _PART_OVERHEAD
        chunk.append(picture)
        chunk_bytes += size
    if chunk:
        bodies.append(([], chunk))
    return [
        pad_json_object(
            {
                "space_id": space_id,
                "part": index,
                "parts": len(bodies),
                "items": items,
                "pictures": pics,
            }
        )
        for index, (items, pics) in enumerate(bodies)
    ]


def parse_bundle_plaintext(raw: bytes) -> ParsedBundlePart | None:
    """One opened part, or ``None`` when it is not a well-formed bundle
    part (shape and counts only — every item and picture is verified by
    the caller)."""
    try:
        body = orjson.loads(raw)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    space_id = body.get("space_id")
    raw_items = body.get("items")
    raw_pictures = body.get("pictures")
    if (
        not isinstance(space_id, str)
        or not space_id
        or not isinstance(raw_items, list)
        or not isinstance(raw_pictures, list)
        or len(raw_items) > MAX_BUNDLE_AUTHORS
        or len(raw_pictures) > MAX_BUNDLE_AUTHORS
    ):
        return None
    items: list[tuple[int, dict]] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            return None
        epoch = entry.get("epoch")
        item = entry.get("item")
        if (
            not isinstance(epoch, int)
            or isinstance(epoch, bool)
            or epoch < 0
            or not isinstance(item, dict)
        ):
            return None
        items.append((epoch, item))
    pictures: list[tuple[str, str, str]] = []
    for entry in raw_pictures:
        if not isinstance(entry, dict):
            return None
        author, digest, picture = (
            entry.get("author"),
            entry.get("hash"),
            entry.get("picture"),
        )
        if not (
            isinstance(author, str)
            and isinstance(digest, str)
            and isinstance(picture, str)
        ):
            return None
        pictures.append((author, digest, picture))
    return ParsedBundlePart(space_id=space_id, items=items, pictures=pictures)
