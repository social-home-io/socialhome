"""Space files held until the row they were sent for arrives.

A ``SPACE_MEDIA_BLOB`` can overtake its row (the post's own event, the
mesh, a seat the roster has not gossiped yet). Its sender can only be bound
to the row once the row is here (:mod:`socialhome.domain.space_media_sender`),
so until then the assembled bytes wait in a folder of their own, keyed by
the sending household and the file name:

    media_dir/.partial/held-<sha256(sender, filename)[:32]>/blob
    media_dir/.partial/held-<…>/meta.json   (sender, space, row, file name)

Nothing under ``.partial`` is ever served, two households' copies of one
name never share a folder, and the media orphan sweep removes a folder left
untouched for three days (``PARTIAL_GRACE_SECONDS``) — the same machinery
an incomplete chunk set uses. When a row of the space lands, the inbound
service re-judges every file held for that space: adopted (moved into
place write-once, :func:`publish_once`), dropped, or kept waiting.
"""

from __future__ import annotations

import hashlib
import logging
import pathlib
import uuid

import aiofiles
import aiofiles.os
import orjson

from ..domain.space_media_sender import HeldSpaceBlob
from .inbound_media_store import drop_parts, publish_once, remove_quietly

log = logging.getLogger(__name__)

#: Folder-name prefix of a held file under ``media_dir/.partial``.
HELD_PREFIX: str = "held-"
#: The held bytes inside a held folder.
BLOB_NAME: str = "blob"
#: What the bytes were sent for, inside a held folder.
META_NAME: str = "meta.json"


def held_key(sender: str, filename: str) -> str:
    """Folder name of ``sender``'s held copy of ``filename``."""
    digest = hashlib.sha256(f"space-held\0{sender}\0{filename}".encode()).hexdigest()
    return f"{HELD_PREFIX}{digest[:32]}"


class HeldSpaceMediaStore:
    """The held files on disk, indexed by space (loaded once, lazily)."""

    __slots__ = ("_by_space", "_loaded", "_root")

    def __init__(self, media_dir: pathlib.Path) -> None:
        self._root = media_dir / ".partial"
        #: ``space_id -> {key: held}``.
        self._by_space: dict[str, dict[str, HeldSpaceBlob]] = {}
        self._loaded = False

    def blob_path(self, sender: str, filename: str) -> pathlib.Path:
        """Where ``sender``'s copy of ``filename`` is assembled."""
        return self._root / held_key(sender, filename) / BLOB_NAME

    async def prepare(self, sender: str, filename: str) -> pathlib.Path:
        """Create the held folder (never through a symlink); its blob path."""
        folder = self._root / held_key(sender, filename)
        for path in (self._root, folder):
            if await aiofiles.os.path.islink(path):
                raise OSError(f"{path.name}: is a symlink — refusing")
        await aiofiles.os.makedirs(folder, exist_ok=True)
        if await aiofiles.os.path.islink(folder):  # pragma: no cover — race
            raise OSError(f"{folder.name}: is a symlink — refusing")
        return folder / BLOB_NAME

    async def record(self, held: HeldSpaceBlob) -> None:
        """Note what a complete held blob was sent for (after it landed)."""
        await self._load()
        folder = self._root / held.key
        tmp = folder / f"meta.tmp-{uuid.uuid4().hex}"
        try:
            async with aiofiles.open(tmp, "wb") as fh:
                await fh.write(orjson.dumps(held.to_dict()))
            await aiofiles.os.replace(tmp, folder / META_NAME)
        finally:
            await remove_quietly(tmp)
        self._by_space.setdefault(held.space_id, {})[held.key] = held

    async def held_for(self, space_id: str) -> list[HeldSpaceBlob]:
        """Every file held for ``space_id``."""
        await self._load()
        return list(self._by_space.get(space_id, {}).values())

    async def adopt(self, held: HeldSpaceBlob, target: pathlib.Path) -> bool:
        """Move the held bytes to ``target`` unless something is there
        already (write-once); the held folder goes either way. ``True``
        when the held copy became ``target``."""
        folder = self._root / held.key
        blob = folder / BLOB_NAME
        try:
            adopted = await aiofiles.os.path.isfile(blob) and await publish_once(
                blob, target
            )
        except OSError as exc:
            log.warning("held space file %s: adopt failed: %s", held.filename, exc)
            adopted = False
        await self.drop(held)
        return adopted

    async def drop(self, held: HeldSpaceBlob) -> None:
        """Remove a held file and forget it."""
        await drop_parts(self._root / held.key)
        bucket = self._by_space.get(held.space_id)
        if bucket is not None:
            bucket.pop(held.key, None)
            if not bucket:
                del self._by_space[held.space_id]

    async def _load(self) -> None:
        """Index what an earlier run left held (the sweep ages it out)."""
        if self._loaded:
            return
        self._loaded = True
        try:
            if await aiofiles.os.path.islink(self._root):
                return
            names = await aiofiles.os.listdir(self._root)
        except OSError:
            return
        for name in names:
            if not name.startswith(HELD_PREFIX):
                continue
            meta = self._root / name / META_NAME
            try:
                if await aiofiles.os.path.islink(self._root / name):
                    continue
                async with aiofiles.open(meta, "rb") as fh:
                    data = orjson.loads(await fh.read())
            except OSError, orjson.JSONDecodeError:
                continue
            held = HeldSpaceBlob.from_dict(name, data)
            if held is not None and held_key(held.sender, held.filename) == name:
                self._by_space.setdefault(held.space_id, {})[name] = held
