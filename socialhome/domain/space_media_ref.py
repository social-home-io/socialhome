"""Where a space file lives in a GFS media cache — the ``media_refs`` descriptor.

A space item (post, calendar event, gallery item) that reaches a household
only through a GFS carries, INSIDE its encrypted payload::

    "media_refs": {"<local filename>": {"blob_id", "suite", "epoch", "size",
                                         "scope", "addr_suite"}}

one entry per file of the item: the address of the sealed blob
(:mod:`socialhome.space_media_seal`), its suite, the content-key epoch it is
sealed under, the blob's byte size, and the GFS scope it was uploaded under
(``space`` or ``channel``) with the address construction (``addr_suite``,
:func:`~socialhome.domain.gfs_space_media.space_media_address`). The address
is bound to the scope: a reader checks it with the scope id it knows ITSELF
(the space id, or its own channel id for a private space) — the descriptor
never names a scope id. No GFS URL — every reader fetches from the GFS it is
connected to. A receiver keeps each entry as a
``space_media_refs`` row (:class:`SpaceMediaRef`) so the file can be fetched
on arrival or when first viewed.

Pure: no I/O.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable
from dataclasses import dataclass

from ..moment_public_media import MAX_BLOB_BYTES, is_blob_id
from ..space_media_seal import UnsupportedSpaceMediaSuite, require_space_media_suite
from .gfs_space_media import MEDIA_SCOPES, SUPPORTED_ADDR_SUITES

log = logging.getLogger(__name__)

#: The payload / record field carrying the descriptor.
MEDIA_REFS_FIELD: str = "media_refs"

_ENTRY_KEYS: frozenset[str] = frozenset(
    {"blob_id", "suite", "epoch", "size", "scope", "addr_suite"}
)
_MAX_EPOCH: int = 2**53


@dataclass(slots=True, frozen=True)
class SpaceMediaRef:
    """One ``space_media_refs`` row: the local file *filename* of
    *space_id* is the blob *blob_id* in a GFS media cache."""

    filename: str
    space_id: str
    blob_id: str
    suite: str
    epoch: int
    size: int
    #: The GFS scope the blob was uploaded under (``space`` / ``channel``)
    #: and how its address binds that scope.
    scope: str
    addr_suite: str
    created_at: str | None = None

    def to_wire(self) -> dict:
        return {
            "blob_id": self.blob_id,
            "suite": self.suite,
            "epoch": self.epoch,
            "size": self.size,
            "scope": self.scope,
            "addr_suite": self.addr_suite,
        }


def media_refs_to_wire(refs: Iterable[SpaceMediaRef]) -> dict[str, dict]:
    """The descriptor for *refs*, keyed by local file name."""
    return {ref.filename: ref.to_wire() for ref in refs}


def _is_int(value: object, low: int, high: int) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
    )


def parse_media_refs(
    raw: object, *, space_id: str, allowed: Collection[str]
) -> list[SpaceMediaRef]:
    """The well-formed entries of a received descriptor.

    *allowed* is the set of local file names the item itself references
    (already validated as safe media names by the caller): an entry for any
    other name is ignored, so a descriptor can never point a fetch at a file
    the item does not show. A descriptor with more entries than *allowed*
    is refused whole. An entry with an unknown suite is refused with a
    WARNING (never a default); any other malformed entry is skipped.
    """
    if not isinstance(raw, dict) or not allowed or len(raw) > len(allowed):
        return []
    out: list[SpaceMediaRef] = []
    for name, entry in raw.items():
        if name not in allowed or not isinstance(entry, dict):
            continue
        if set(entry) != _ENTRY_KEYS:
            continue
        try:
            suite = require_space_media_suite(entry.get("suite"))
        except UnsupportedSpaceMediaSuite as exc:
            log.warning("space media descriptor for %s refused: %s", name, exc)
            continue
        addr_suite = entry.get("addr_suite")
        if addr_suite not in SUPPORTED_ADDR_SUITES:
            log.warning(
                "space media descriptor for %s refused: address suite %r",
                name,
                addr_suite,
            )
            continue
        scope = entry.get("scope")
        if scope not in MEDIA_SCOPES:
            continue
        blob_id = entry.get("blob_id")
        epoch = entry.get("epoch")
        size = entry.get("size")
        if not is_blob_id(blob_id) or not _is_int(epoch, 0, _MAX_EPOCH):
            continue
        if not _is_int(size, 1, MAX_BLOB_BYTES):
            continue
        out.append(
            SpaceMediaRef(
                filename=name,
                space_id=space_id,
                blob_id=str(blob_id),
                suite=suite,
                epoch=int(epoch),  # type: ignore[arg-type]
                size=int(size),  # type: ignore[arg-type]
                scope=str(scope),
                addr_suite=str(addr_suite),
            )
        )
    return out


__all__ = [
    "MEDIA_REFS_FIELD",
    "SpaceMediaRef",
    "media_refs_to_wire",
    "parse_media_refs",
]
