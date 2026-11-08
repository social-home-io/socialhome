"""SHA-256 of this household's stored media files — the ``files`` map (v_85).

A sender describes each file of a space item it sends by the hash of the
bytes it stores (:mod:`socialhome.domain.file_hashes`); a §25.6 provider
acting as host does the same for the records it streams, and the
``SPACE_MEDIA_BLOB`` sender for the whole file it ships. Hashing reads the
file through ``aiofiles`` in parts and runs each ``update`` in a worker
thread, so a large video never blocks the event loop. A small cache keyed
by ``(name, size, mtime_ns)`` keeps a file sent to many households (or
first described, then shipped) from being hashed again.

Fail-soft: a missing or unreadable file is left out of the map (the item
then takes the legacy names for it).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import pathlib
from collections import OrderedDict
from collections.abc import Iterable

import aiofiles
import aiofiles.os

from ..domain.file_hashes import MAX_FILE_HASH_ENTRIES, FileHash, file_hashes_to_wire
from .inbound_media_store import media_basename, media_file_path

log = logging.getLogger(__name__)

#: Bytes read (and hashed in one worker-thread hop) at a time.
READ_BYTES: int = 1024 * 1024

#: Hashes kept in memory, newest last.
CACHE_ENTRIES: int = 4096


class MediaFileHasher:
    """The ``files`` map of the local files an item references."""

    __slots__ = ("_cache", "_media_dir")

    def __init__(self, media_dir: pathlib.Path) -> None:
        self._media_dir = media_dir
        self._cache: OrderedDict[tuple[str, int, int], FileHash] = OrderedDict()

    async def hash_name(self, name: str) -> FileHash | None:
        """The hash of the stored file *name*, or ``None`` (unsafe name,
        missing, unreadable)."""
        path = media_file_path(self._media_dir, name)
        if path is None:
            return None
        try:
            st = await aiofiles.os.stat(path)
        except OSError:
            return None
        key = (name, st.st_size, st.st_mtime_ns)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        try:
            digest = hashlib.sha256()
            size = 0
            async with aiofiles.open(path, "rb") as fh:
                while part := await fh.read(READ_BYTES):
                    size += len(part)
                    await asyncio.to_thread(digest.update, part)
        except OSError as exc:
            log.warning("file hash: could not read %s: %s", name, exc)
            return None
        found = FileHash(digest.hexdigest(), size)
        self._cache[key] = found
        while len(self._cache) > CACHE_ENTRIES:
            self._cache.popitem(last=False)
        return found

    async def hashes_for(self, urls: Iterable[str | None]) -> dict[str, FileHash]:
        """The map for *urls* (local ``api/media/`` references), by file
        name, each file once, at most :data:`MAX_FILE_HASH_ENTRIES` — the
        first ones, in order (a longer map is refused by every receiver)."""
        out: dict[str, FileHash] = {}
        for url in urls:
            if len(out) >= MAX_FILE_HASH_ENTRIES:
                break
            name = media_basename(url)
            if not name or name in out:
                continue
            found = await self.hash_name(name)
            if found is not None:
                out[name] = found
        return out

    async def with_hashes(self, payload: dict, urls: Iterable[str | None]) -> dict:
        """*payload* plus the ``files`` map of *urls* (a new dict), or
        *payload* itself when no file could be hashed."""
        wire = file_hashes_to_wire(await self.hashes_for(urls))
        return {**payload, **wire} if wire else payload

    @staticmethod
    async def attach(
        hasher: MediaFileHasher | None, payload: dict, urls: Iterable[str | None]
    ) -> dict:
        """:meth:`with_hashes` when a hasher is wired, else *payload*;
        fail-soft (a WARNING and the payload unchanged)."""
        if hasher is None:
            return payload
        try:
            return await hasher.with_hashes(payload, urls)
        except Exception:
            log.warning("file hashes could not be attached", exc_info=True)
            return payload
