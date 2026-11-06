"""Shared handling of encrypted, content-addressed blobs on a GFS.

Two stores keep ciphertext this server can't open: public-moment media
(:mod:`.moment_media`, v_63) and the space media cache (:mod:`.space_media`).
Both take the same blob shape — a padded bucket
(:data:`~socialhome.moment_public_media.PAD_BUCKETS`) + nonce + tag, addressed
by its SHA-256 — and keep the bytes as files. What they share lives here:

* :func:`check_shape` / :func:`check_address` — the upload checks every blob
  passes, whatever its store (422 :class:`MediaRefused` otherwise);
* :class:`BlobFiles` — the small storage interface a store writes through,
  and :class:`FsBlobFiles`, the filesystem one. The data dir is shared by
  every node of a cluster, so a blob one node took is served by any other.
  An object store can replace the filesystem by implementing
  :class:`BlobFiles`; nothing else changes.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from pathlib import Path
from typing import Protocol

import aiofiles
import aiofiles.os

from ..moment_public_media import MAX_BLOB_BYTES, PAD_BUCKETS, is_blob_id

log = logging.getLogger(__name__)

#: AES-GCM nonce + tag around a padded bucket.
BLOB_OVERHEAD: int = 12 + 16

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
    if len(data) > MAX_BLOB_BYTES or len(data) not in BUCKET_SIZES:
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
    async def scan(self, *, limit: int) -> list[tuple[str, float]]: ...


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

    async def scan(self, *, limit: int) -> list[tuple[str, float]]:
        """Up to *limit* ``(name, mtime)`` of what the directory holds —
        blobs and temporary files — for the orphan sweep."""
        try:
            names = await aiofiles.os.listdir(self._dir)
        except FileNotFoundError:
            return []
        out: list[tuple[str, float]] = []
        for name in names[:limit]:
            try:
                st = await aiofiles.os.stat(self._dir / name)
            except FileNotFoundError:
                continue
            out.append((name, float(st.st_mtime)))
        return out

    async def _discard(self, path: Path) -> None:
        try:
            await aiofiles.os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as exc:  # pragma: no cover - defensive
            log.warning("blob store: could not remove a blob file: %s", exc)
