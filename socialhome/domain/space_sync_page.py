"""The sync page a household holds for a space (migration 0151).

``docs/protocol/sync-pages.md``. The row as stored: the signed header as
canonical JSON, its 8 KiB bucket vector and — on the signer only — the
delta since its previous page. Parsing and verification live in
:mod:`socialhome.sync_page`; this is the plain row.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class StoredSyncPage:
    """One ``space_sync_pages`` row."""

    space_id: str
    page_json: str
    vector: bytes
    delta: bytes | None = None


__all__ = ["StoredSyncPage"]
