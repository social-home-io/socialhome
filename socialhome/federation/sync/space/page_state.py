"""A space's bucket vector, computed from this household's own rows.

``docs/protocol/sync-pages.md`` §1–§2. The **same** code runs on the signer
(sealing a page) and on a member (checking its copy against the stored
page): the committed resources' §25.6 exporters, read in full and page by
page, each record turned into its leaf (:mod:`socialhome.sync_page`). No
leaf cache (owner decision 4): leaves are recomputed every time, and the
only memory held is 32 bytes per committed row.

* The retention window is the page's ``cutoff``, never this household's
  clock (:func:`~.window.pinned_cutoff`), so both sides evaluate one set.
* Files are committed by content: each record's file references are hashed
  from the bytes this household stores (the v_85 :class:`MediaFileHasher`,
  cached by name, size and mtime). A file it cannot hash keeps the row out
  (rule 3 — availability, never safety).
* Hashing runs in a worker thread, one exporter page at a time; the export
  itself is the exporters' async keyset reads.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol

from ....services.inbound_media_store import media_basename
from ....sync_page import COMMITTED_RESOURCES, VectorBuilder, file_refs
from .exporter import RESOURCE_ORDER, record_batches
from .window import pinned_cutoff

if TYPE_CHECKING:
    from ....domain.file_hashes import FileHash
    from .exporter import ResourceExporter
    from .window import SyncWindows

log = logging.getLogger(__name__)


class _FileHasher(Protocol):
    async def hash_name(self, name: str) -> FileHash | None: ...


class _Unset:
    pass


_NOW = _Unset()


class PageStateHasher:
    """Computes the bucket vector of a space from the local exporters."""

    __slots__ = ("_exporters", "_files", "_windows")

    def __init__(
        self,
        *,
        exporters: Mapping[str, ResourceExporter],
        windows: SyncWindows | None,
        files: _FileHasher | None,
    ) -> None:
        self._exporters = exporters
        self._windows = windows
        #: ``None``: no file can be hashed — every row with a file stays out.
        self._files = files

    async def cutoff(self, space_id: str) -> str | None:
        """The retention cutoff a page sealed now commits with."""
        if self._windows is None:
            return None
        return (await self._windows.for_space(space_id)).cutoff

    async def vector(
        self, space_id: str, *, cutoff: str | None | _Unset = _NOW
    ) -> tuple[bytes, int]:
        """``(vector, rows)`` of *space_id*: the 256 × 32-byte bucket vector
        and how many rows it commits. *cutoff*: the page's cutoff (default:
        the space's window now)."""
        pin = await self.cutoff(space_id) if isinstance(cutoff, _Unset) else cutoff
        builder = VectorBuilder()
        with pinned_cutoff(space_id, pin):
            for resource in RESOURCE_ORDER:
                if resource not in COMMITTED_RESOURCES:
                    continue
                exporter = self._exporters.get(resource)
                if exporter is None:
                    continue
                async for batch in record_batches(exporter, space_id):
                    hashes = await self._hash_files(resource, batch)
                    await asyncio.to_thread(_add_all, builder, resource, batch, hashes)
        vector = await asyncio.to_thread(builder.finish)
        return vector, builder.rows

    async def _hash_files(
        self, resource: str, batch: list[dict[str, Any]]
    ) -> dict[str, str]:
        out: dict[str, str] = {}
        if self._files is None:
            return out
        for record in batch:
            for url in file_refs(resource, record):
                if url in out:
                    continue
                name = media_basename(url)
                found = await self._files.hash_name(name) if name else None
                if found is not None:
                    out[url] = found.h
        return out


def _add_all(
    builder: VectorBuilder,
    resource: str,
    batch: list[dict[str, Any]],
    hashes: dict[str, str],
) -> None:
    for record in batch:
        builder.add(resource, record, hashes.get)


__all__ = ["PageStateHasher"]
