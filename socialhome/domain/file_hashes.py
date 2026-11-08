"""File hashes in space content — the ``files`` map (v_85).

Every space item that shows files (a post's pictures, video, file and
link-card thumbnail, a comment's picture, a calendar cover, a gallery
item's file and thumbnail, a bazaar listing's photos, a page's cover and
body pictures, a held moderation item's files) carries, INSIDE its
encrypted payload and in each §25.6 record::

    "files": {"<author filename>": {"h": "<64 hex>"}},
    "files_suite": "sha256"

the SHA-256 of each file's bytes as its sender stores them — no size: a
receiver could not check one against the bytes before they land, and the
hash already refuses a truncated file (an older draft's ``size`` is
ignored). Who vouches
for it is who vouches for the item: the author household's envelope
signature for a live event, the host (or a delegated admin) for a §25.6
catch-up record — which may attach hashes to content from before v_85,
computed from its own copy.

A receiver stores a hashed file content-addressed:
:func:`space_content_media_name` ``(space, h)`` — ``sc-`` + a digest of
the space and the hash. Whatever is stored under that name hashes to
``h`` (every write path checks), so no member can plant bytes under
another's file: an item naming somebody else's file only ever reaches the
bytes that hash to what IT claims. The space id in the name keeps two
spaces' copies apart.

**Suite.** :data:`FILE_HASH_SUITE_SHA256` rides every map; a receiver
rejects a map with an unknown or missing suite whole — the item then takes
the legacy (unhashed) names — never a default.

Pure: no I/O.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass

from .media_constraints import SPACE_CONTENT_MEDIA_PREFIX

log = logging.getLogger(__name__)

#: The payload / record field carrying the map, and its suite tag.
FILES_FIELD: str = "files"
FILES_SUITE_FIELD: str = "files_suite"

#: The ``SPACE_MEDIA_BLOB`` fields carrying the whole file's hash.
BLOB_HASH_FIELD: str = "h"
BLOB_HASH_SUITE_FIELD: str = "h_suite"

FILE_HASH_SUITE_SHA256: str = "sha256"
SUPPORTED_FILE_HASH_SUITES: frozenset[str] = frozenset({FILE_HASH_SUITE_SHA256})

#: Entries one map may carry (a post shows ≤ 10 pictures, a file and a
#: link card; a page version ≤ 16 files): past it the map is refused whole.
MAX_FILE_HASH_ENTRIES: int = 16

_HEX64 = re.compile(r"[0-9a-f]{64}")
#: The author's file name: one safe path component (the shape
#: ``inbound_media_store.SAFE_MEDIA_NAME`` accepts).
_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._:-]{0,199}")


class UnsupportedFileHashSuite(ValueError):
    """A ``files_suite`` / ``h_suite`` this household does not know."""


@dataclass(slots=True, frozen=True)
class FileHash:
    """One file's SHA-256 (64 lowercase hex) and — for this household's own
    copy only, never on the wire — its byte size (``-1``: not known)."""

    h: str
    size: int = -1


def require_file_hash_suite(suite: object) -> str:
    """*suite* when supported, else raise :class:`UnsupportedFileHashSuite`."""
    if not isinstance(suite, str) or suite not in SUPPORTED_FILE_HASH_SUITES:
        raise UnsupportedFileHashSuite(f"unsupported file hash suite {suite!r}")
    return suite


def is_file_hash(value: object) -> bool:
    """``True`` for a SHA-256 in the wire shape (64 lowercase hex)."""
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def sha256_hex(data: bytes) -> str:
    """The wire hash of *data*. CPU-bound on a large buffer: callers in
    ``async def`` run it through ``asyncio.to_thread``."""
    return hashlib.sha256(data).hexdigest()


def file_hashes_to_wire(hashes: Mapping[str, FileHash]) -> dict:
    """The ``files`` + ``files_suite`` fields for *hashes* (merge into a
    payload / record), or ``{}`` when there is nothing to describe."""
    if not hashes:
        return {}
    return {
        FILES_FIELD: {name: {"h": fh.h} for name, fh in hashes.items()},
        FILES_SUITE_FIELD: FILE_HASH_SUITE_SHA256,
    }


def parse_file_hashes(payload: Mapping) -> dict[str, FileHash]:
    """The hash map an item (payload or §25.6 record) carries, by its
    author's file names — ``{}`` when it carries none or the map is
    refused: an unknown / missing suite, not a mapping, more than
    :data:`MAX_FILE_HASH_ENTRIES` entries, or ANY bad entry (an unsafe
    name, a hash not 64 lowercase hex). Refused whole: the item then takes
    the legacy names, never a partial map. Any other key of an entry is
    ignored."""
    files = payload.get(FILES_FIELD)
    if files is None:
        return {}
    try:
        require_file_hash_suite(payload.get(FILES_SUITE_FIELD))
    except UnsupportedFileHashSuite as exc:
        log.warning("file hashes refused: %s", exc)
        return {}
    if not isinstance(files, Mapping) or len(files) > MAX_FILE_HASH_ENTRIES:
        log.warning(
            "file hashes refused: not a map of at most %d", MAX_FILE_HASH_ENTRIES
        )
        return {}
    out: dict[str, FileHash] = {}
    for name, entry in files.items():
        if (
            not isinstance(name, str)
            or _NAME.fullmatch(name) is None
            or not isinstance(entry, Mapping)
            or not is_file_hash(entry.get("h"))
        ):
            log.warning("file hashes refused: bad entry %r", name)
            return {}
        out[name] = FileHash(entry["h"])
    return out


def blob_hash(payload: Mapping) -> str | None:
    """The whole file's hash a ``SPACE_MEDIA_BLOB`` carries (``h`` under a
    supported ``h_suite``), or ``None`` — a missing or unknown suite or a
    malformed hash is a legacy (unhashed) blob."""
    h = payload.get(BLOB_HASH_FIELD)
    if h is None:
        return None
    try:
        require_file_hash_suite(payload.get(BLOB_HASH_SUITE_FIELD))
    except UnsupportedFileHashSuite:
        return None
    return h if is_file_hash(h) else None


def blob_hash_fields(h: str) -> dict[str, str]:
    """The ``SPACE_MEDIA_BLOB`` fields naming the whole file's hash *h*."""
    return {BLOB_HASH_FIELD: h, BLOB_HASH_SUITE_FIELD: FILE_HASH_SUITE_SHA256}


def space_content_media_name(space_id: str, h: str, filename: str) -> str:
    """Where a household stores the file of space *space_id* whose SHA-256
    is *h*: ``sc-`` + ``sha256("space-content", space, h)[:40]`` + the
    extension of *filename* (≤ 10 chars). The bytes under it hash to *h*
    whoever delivered them, so the name needs no sender binding; the space
    id keeps a file of one space from ever matching another space's.
    Fixed length, always a safe media name."""
    digest = hashlib.sha256(f"space-content\0{space_id}\0{h}".encode()).hexdigest()
    dot = filename.rfind(".")
    ext = filename[dot:] if 0 < dot and len(filename) - dot <= 10 else ""
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,9}", ext):
        ext = ""
    return f"{SPACE_CONTENT_MEDIA_PREFIX}{digest[:40]}{ext}"


def is_space_content_media_name(name: str) -> bool:
    """``True`` for a :func:`space_content_media_name`."""
    return name.startswith(SPACE_CONTENT_MEDIA_PREFIX)
