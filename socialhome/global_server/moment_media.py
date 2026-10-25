"""GFS store for encrypted public-moment media (§Momentum-public, v_63).

The author's household uploads each moment file once, encrypted
(:mod:`socialhome.moment_public_media`); followers and share-link viewers
fetch it here by its SHA-256 address. This server holds ciphertext it can't
open, for no longer than the moment can live.

Bounds (every one enforced here, whatever the uploader claims):

* a blob is a padded bucket (:data:`~socialhome.moment_public_media.PAD_BUCKETS`)
  + nonce + tag, ≤ :data:`~socialhome.moment_public_media.MAX_BLOB_BYTES`;
* its address is the SHA-256 of the bytes received;
* it expires no later than :data:`MAX_TTL_S` from now;
* an author holds at most :data:`MAX_AUTHOR_LIVE_BYTES` and uploads at most
  :data:`MAX_UPLOADS_PER_HOUR` blobs an hour;
* expired blobs (rows and files) go on each maintenance sweep.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

import aiofiles
import aiofiles.os

from ..moment_public_media import MAX_BLOB_BYTES, PAD_BUCKETS, is_blob_id
from .domain import GfsMomentMedia

if TYPE_CHECKING:
    from .repositories import AbstractGfsMomentMediaRepo

log = logging.getLogger(__name__)

#: A blob is kept for delivery only: followers fetch it when the moment
#: reaches them (an envelope is refused 25 h after it was sent) and keep
#: their own copy, so a moment living up to 30 days (v_65) needs no longer
#: blob here. Authors ask ≤ 7 days; a blob a day past that is refused.
MAX_TTL_S: int = 8 * 86_400

#: Live ciphertext one author may hold here.
MAX_AUTHOR_LIVE_BYTES: int = 256 * 1024 * 1024

#: Blobs one author may upload per hour (a moment has ≤ 2: file + poster).
MAX_UPLOADS_PER_HOUR: int = 60

#: Expired blobs removed per sweep.
PRUNE_BATCH: int = 500

_OVERHEAD = 12 + 16  # nonce + tag
_BUCKET_SIZES = frozenset(b + _OVERHEAD for b in PAD_BUCKETS)


class MediaRefused(Exception):
    """An upload this server won't keep. ``status`` is the HTTP answer."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class GfsMomentMediaStore:
    """Encrypted moment blobs: files under ``media_dir/moments``, metadata
    in ``gfs_moment_media``."""

    __slots__ = ("_dir", "_repo")

    def __init__(self, repo: AbstractGfsMomentMediaRepo, media_dir: str | Path) -> None:
        self._repo = repo
        self._dir = Path(media_dir) / "moments"

    def _path(self, blob_id: str) -> Path:
        return self._dir / blob_id

    async def put(
        self,
        *,
        blob_id: str,
        author_user_id: str,
        data: bytes,
        expires_at: int,
        now: int | None = None,
    ) -> bool:
        """Keep one blob. ``True`` when stored, ``False`` when it already was
        (a retry). Raises :class:`MediaRefused`."""
        now = int(time.time()) if now is None else now
        if not is_blob_id(blob_id):
            raise MediaRefused(422, "bad_blob_id")
        if len(data) > MAX_BLOB_BYTES or len(data) not in _BUCKET_SIZES:
            raise MediaRefused(422, "not_a_padded_blob")
        if not now < expires_at <= now + MAX_TTL_S:
            raise MediaRefused(422, "bad_expiry")
        digest = await asyncio.to_thread(lambda: hashlib.sha256(data).hexdigest())
        if digest != blob_id:
            raise MediaRefused(422, "blob_id_mismatch")
        held = await self._repo.get(blob_id)
        if held is not None:
            if held.author_user_id != author_user_id:
                raise MediaRefused(409, "blob_id_taken")
            return False
        live, recent = await self._repo.author_usage(author_user_id, since=now - 3600)
        if recent >= MAX_UPLOADS_PER_HOUR or live + len(data) > MAX_AUTHOR_LIVE_BYTES:
            raise MediaRefused(429, "author_media_quota")
        await aiofiles.os.makedirs(self._dir, exist_ok=True)
        tmp = self._dir / f".{blob_id}.part"
        async with aiofiles.open(tmp, "wb") as fh:
            await fh.write(data)
        await aiofiles.os.replace(tmp, self._path(blob_id))
        await self._repo.add(
            GfsMomentMedia(
                blob_id=blob_id,
                author_user_id=author_user_id,
                size=len(data),
                expires_at=expires_at,
            )
        )
        return True

    async def read(self, blob_id: str, *, now: int | None = None) -> bytes | None:
        """The blob, or ``None`` when unknown or expired."""
        if not is_blob_id(blob_id):
            return None
        now = int(time.time()) if now is None else now
        held = await self._repo.get(blob_id)
        if held is None or held.expires_at <= now:
            return None
        try:
            async with aiofiles.open(self._path(blob_id), "rb") as fh:
                return await fh.read()
        except OSError:
            return None

    async def delete(self, blob_id: str, *, author_user_id: str) -> bool:
        if not is_blob_id(blob_id):
            return False
        if not await self._repo.delete(blob_id, author_user_id=author_user_id):
            return False
        await self._unlink(blob_id)
        return True

    async def prune_expired(self, now: int | None = None) -> int:
        now = int(time.time()) if now is None else now
        ids = await self._repo.expired(now, limit=PRUNE_BATCH)
        for blob_id in ids:
            await self._unlink(blob_id)
        await self._repo.delete_ids(ids)
        return len(ids)

    async def purge_author(self, author_user_id: str) -> int:
        """Every blob of an author who left the directory."""
        ids = await self._repo.ids_for_author(author_user_id)
        for blob_id in ids:
            await self._unlink(blob_id)
        await self._repo.delete_ids(ids)
        return len(ids)

    async def _unlink(self, blob_id: str) -> None:
        try:
            await aiofiles.os.remove(self._path(blob_id))
        except FileNotFoundError:
            pass
        except OSError as exc:  # pragma: no cover - defensive
            log.warning("moment media: could not remove a blob file: %s", exc)
