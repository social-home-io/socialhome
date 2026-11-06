"""Shared handling of encrypted, content-addressed blobs on a GFS.

Two stores keep ciphertext this server can't open: public-moment media
(:mod:`.moment_media`, v_63) and the space media cache (:mod:`.space_media`).
Both take the same blob shape — a padded bucket
(:data:`~socialhome.moment_public_media.PAD_BUCKETS`) + nonce + tag, addressed
by its SHA-256 — and keep the bytes as files. What they share lives here:

* :func:`check_shape` / :func:`check_address` — the upload checks every blob
  passes, whatever its store (422 :class:`MediaRefused` otherwise);
* :func:`remove_stale_temps` — the temporary files a write that died
  before its rename left behind, removed once older than a grace;
* :class:`BlobFiles` — the small storage interface a store writes through,
  and :class:`FsBlobFiles`, the filesystem one. The data dir is shared by
  every node of a cluster, so a blob one node took is served by any other.
  An object store can replace the filesystem by implementing
  :class:`BlobFiles`; nothing else changes.
"""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import logging
import os
import secrets
from pathlib import Path
from typing import Protocol

import aiofiles
import aiofiles.os

from ..moment_public_media import MAX_BLOB_BYTES, PAD_BUCKETS, is_blob_id

log = logging.getLogger(__name__)

#: AES-GCM nonce + tag around a padded bucket.
BLOB_OVERHEAD: int = 12 + 16

#: A temporary file (``.{blob_id}.{token}.part``) older than this is a
#: write that died before its rename — no upload takes this long.
TEMP_GRACE_S: int = 600

#: One scan page: ``(name, mtime)`` entries, and the name to continue after
#: (``""`` once the end of the directory is reached).
ScanPage = tuple[list[tuple[str, float]], str]

#: Every size a well-formed blob can have.
BUCKET_SIZES: frozenset[int] = frozenset(b + BLOB_OVERHEAD for b in PAD_BUCKETS)


class MediaRefused(Exception):
    """An upload this server won't keep. ``status`` is the HTTP answer."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def check_shape(blob_id: str, data: bytes) -> None:
    """A well-formed address and a padded-bucket size, else 422."""
    if not is_blob_id(blob_id):
        raise MediaRefused(422, "bad_blob_id")
    check_size(len(data))


def check_size(size: int) -> None:
    """A padded-bucket size, else 422 — checkable from a signed size before
    any body is read."""
    if size > MAX_BLOB_BYTES or size not in BUCKET_SIZES:
        raise MediaRefused(422, "not_a_padded_blob")


async def check_address(blob_id: str, data: bytes) -> None:
    """The blob's address is the SHA-256 of the bytes received, else 422.
    Hashing up to 8 MB runs in a worker thread."""
    digest = await asyncio.to_thread(_sha256_hex, data)
    if digest != blob_id:
        raise MediaRefused(422, "blob_id_mismatch")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class BlobFiles(Protocol):
    """Where a store keeps a blob's bytes, by its address."""

    async def write(self, blob_id: str, data: bytes) -> None: ...
    async def read(self, blob_id: str) -> bytes | None: ...
    async def remove(self, blob_id: str) -> None: ...
    async def stat(self, name: str) -> float | None: ...
    async def exists(self, blob_id: str) -> bool: ...
    async def scan(self, *, after: str, limit: int) -> ScanPage: ...


class FsBlobFiles:
    """:class:`BlobFiles` as files named by their address under one dir.

    A write goes to a temporary file of its own (a random suffix) and is
    renamed into place, so a reader never sees half a blob and two nodes
    taking the same upload at once never truncate each other's file — the
    last rename wins, with the same bytes."""

    __slots__ = ("_dir",)

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)

    def _path(self, blob_id: str) -> Path:
        return self._dir / blob_id

    async def write(self, blob_id: str, data: bytes) -> None:
        await aiofiles.os.makedirs(self._dir, exist_ok=True)
        tmp = self._dir / f".{blob_id}.{secrets.token_hex(8)}.part"
        try:
            async with aiofiles.open(tmp, "wb") as fh:
                await fh.write(data)
            await aiofiles.os.replace(tmp, self._path(blob_id))
        except BaseException:
            await self._discard(tmp)
            raise

    async def read(self, blob_id: str) -> bytes | None:
        try:
            async with aiofiles.open(self._path(blob_id), "rb") as fh:
                return await fh.read()
        except OSError:
            return None

    async def remove(self, blob_id: str) -> None:
        await self._discard(self._path(blob_id))

    async def stat(self, name: str) -> float | None:
        """The mtime of *name* (a blob or a temporary file), or ``None``."""
        try:
            st = await aiofiles.os.stat(self._dir / name)
        except FileNotFoundError:
            return None
        return float(st.st_mtime)

    async def exists(self, blob_id: str) -> bool:
        return await self.stat(blob_id) is not None

    async def scan(self, *, after: str, limit: int) -> ScanPage:
        """The next *limit* names in name order after *after* — blobs and
        temporary files — with their mtimes, and the name to continue after:
        the last one LISTED (a file gone before its stat drops out of the
        entries but not out of the walk), or ``""`` at the end. So a sweep
        walks the whole directory over successive calls however busy it is.
        The directory is iterated in a worker thread keeping only the
        *limit* smallest names, so memory is bounded by *limit*, not by the
        directory."""
        return await asyncio.to_thread(_scan_sync, self._dir, after, limit)

    async def _discard(self, path: Path) -> None:
        try:
            await aiofiles.os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as exc:  # pragma: no cover - defensive
            log.warning("blob store: could not remove a blob file: %s", exc)


def is_temp_name(name: str) -> bool:
    """A write's temporary file (``.{blob_id}.{token}.part``)."""
    return name.startswith(".") and name.endswith(".part")


async def remove_stale_temps(
    files: BlobFiles, entries: list[tuple[str, float]], *, cutoff: float
) -> int:
    """Remove the temporary files among *entries* (a :meth:`BlobFiles.scan`
    page) last written before *cutoff* — each re-stat'ed right before its
    unlink, so one written meanwhile stays. Temporary names are random per
    write, so no other upload (on any node) can take one over. Returns the
    files removed."""
    removed = 0
    for name, mtime in entries:
        if not is_temp_name(name) or mtime >= cutoff:
            continue
        current = await files.stat(name)
        if current is not None and current == mtime:
            await files.remove(name)
            removed += 1
    return removed


def _scan_sync(directory: Path, after: str, limit: int) -> ScanPage:
    """:meth:`FsBlobFiles.scan`'s blocking body (runs in a worker thread)."""
    try:
        with os.scandir(directory) as entries:
            names = heapq.nsmallest(limit, (e.name for e in entries if e.name > after))
    except FileNotFoundError:
        return [], ""
    out: list[tuple[str, float]] = []
    for name in names:
        try:
            out.append((name, os.stat(directory / name).st_mtime))
        except FileNotFoundError:
            continue
    # A full page may have more after it; a short one reached the end.
    return out, (names[-1] if len(names) >= limit else "")
