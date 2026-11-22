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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from ....domain.file_hashes import FileHash, file_hashes_to_wire
from ....services.inbound_media_store import media_basename
from ....sync_page import (
    COMMITTED_RESOURCES,
    COUNTERPARTS,
    HINT_PREFIX_BYTES,
    MAX_FETCH_ROWS,
    ListingRow,
    VectorBuilder,
    change_time,
    file_refs,
    record_leaf,
    row_id_of,
    row_key,
    superseded_key,
)
from .exporter import RESOURCE_ORDER, record_batches
from .window import pinned_cutoff

if TYPE_CHECKING:
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

    async def rows_in(
        self,
        space_id: str,
        buckets: Iterable[int],
        *,
        cutoff: str | None,
        keep_records: bool = False,
        skip: frozenset[bytes] = frozenset(),
        keep_ids: frozenset[tuple[str, str]] = frozenset(),
        known: Mapping[str, str] | None = None,
    ) -> BucketRows:
        """The committed rows of *buckets* (with the page's *cutoff*): per
        bucket its rows as :class:`~socialhome.sync_page.ListingRow`
        (resource, id, leaf, change time); and every live row this household
        holds as gone — deleted, archived — (:data:`SUPERSEDED_BY`). One
        export of the space, like :meth:`vector`; only the asked buckets'
        rows are kept.

        *keep_records* (a v_95 deliverer, Phase 3): also each row's record —
        with the content hashes of its files (``files``) — unless its leaf's
        8-byte prefix is in *skip* (the requester holds it), at most
        :data:`~socialhome.sync_page.MAX_FETCH_ROWS` per bucket.

        A requester (Phase 3): *keep_ids* — our own records of those
        identities, in any bucket (``own``: their uncommitted columns carry
        over when a verified version is applied); *known* — content names
        whose hash we know though we may not hold the bytes yet (``name ->
        SHA-256``: a file of a row we just applied). Every row of a
        resource with a live / archived counterpart is noted with its
        change time in ``held_any`` (:data:`COUNTERPARTS`)."""
        wanted = frozenset(buckets)
        out = BucketRows(
            rows={i: [] for i in wanted},
            gone=set(),
            records={i: [] for i in wanted} if keep_records else None,
            skip=skip,
            keep_ids=keep_ids,
        )
        with pinned_cutoff(space_id, cutoff):
            for resource in RESOURCE_ORDER:
                if resource not in COMMITTED_RESOURCES:
                    continue
                exporter = self._exporters.get(resource)
                if exporter is None:
                    continue
                async for batch in record_batches(exporter, space_id):
                    hashes = await self._hash_files(resource, batch, known)
                    await asyncio.to_thread(
                        _collect, out, wanted, resource, batch, hashes
                    )
        return out

    async def _hash_files(
        self,
        resource: str,
        batch: list[dict[str, Any]],
        known: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        out: dict[str, str] = {}
        for record in batch:
            for url in file_refs(resource, record):
                if url in out:
                    continue
                name = media_basename(url)
                if known is not None and name and name in known:
                    out[url] = known[name]
                    continue
                if self._files is None:
                    continue
                found = await self._files.hash_name(name) if name else None
                if found is not None:
                    out[url] = found.h
        return out


@dataclass(slots=True)
class BucketRows:
    """:meth:`PageStateHasher.rows_in` — working data, never stored."""

    rows: dict[int, list[ListingRow]]
    gone: set[tuple[str, str]]
    #: Per bucket ``(resource, record)`` a deliverer may send (Phase 3);
    #: ``None`` unless asked for.
    records: dict[int, list[tuple[str, dict[str, Any]]]] | None = None
    #: Leaf prefixes whose records are not kept (the requester holds them).
    skip: frozenset[bytes] = frozenset()
    #: Phase 3 requester: the identities whose own records are kept …
    keep_ids: frozenset[tuple[str, str]] = frozenset()
    #: … those records, by identity (whatever bucket) …
    own: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    #: … and every held row of a resource with a live / archived counterpart,
    #: by identity, with its change time.
    held_any: dict[tuple[str, str], str | None] = field(default_factory=dict)


def _with_files(
    resource: str, record: dict[str, Any], hashes: dict[str, str]
) -> dict[str, Any]:
    """*record* with the ``files`` hash map of the files its commit form
    hashes — keyed by file name, as every v_85 sender keys it."""
    found = {
        name: FileHash(h)
        for url in file_refs(resource, record)
        if (h := hashes.get(url)) and (name := media_basename(url))
    }
    return {**record, **file_hashes_to_wire(found)} if found else record


def _collect(
    out: BucketRows,
    wanted: frozenset[int],
    resource: str,
    batch: list[dict[str, Any]],
    hashes: dict[str, str],
) -> None:
    for record in batch:
        row_id = row_id_of(resource, record)
        if row_id is None:
            continue
        key = superseded_key(resource, row_id)
        if key is not None:
            out.gone.add(key)
        if resource in COUNTERPARTS:
            out.held_any[(resource, row_id)] = change_time(record)
        if (resource, row_id) in out.keep_ids:
            out.own[(resource, row_id)] = record
        index = row_key(resource, row_id)[0]
        if index not in wanted:
            continue
        found = record_leaf(resource, record, hashes.get)
        if found is not None:
            out.rows[index].append(
                ListingRow(resource, row_id, found[1], change_time(record))
            )
            kept = out.records.get(index) if out.records is not None else None
            if (
                kept is not None
                and len(kept) < MAX_FETCH_ROWS
                and found[1][:HINT_PREFIX_BYTES] not in out.skip
            ):
                kept.append((resource, _with_files(resource, record, hashes)))


def _add_all(
    builder: VectorBuilder,
    resource: str,
    batch: list[dict[str, Any]],
    hashes: dict[str, str],
) -> None:
    for record in batch:
        builder.add(resource, record, hashes.get)


__all__ = ["BucketRows", "PageStateHasher"]
