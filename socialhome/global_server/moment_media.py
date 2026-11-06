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

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ..moment_public_media import is_blob_id
from .blob_store import FsBlobFiles, MediaRefused, check_address, check_shape
from .domain import GfsMomentMedia

if TYPE_CHECKING:
    from .repositories import AbstractGfsMomentMediaRepo

log = logging.getLogger(__name__)

#: Longest a blob is kept. Followers fetch it when the moment reaches them
#: and keep their own copy, but a share link's viewer fetches it here for
#: as long as the moment lives — up to 30 days (v_65). So a blob may live
#: that long (v_66, ``moment_media_30d``; an older server allowed 8 days),
#: plus a day of clock slack. The per-author live-bytes cap
#: (:data:`MAX_AUTHOR_LIVE_BYTES`) bounds what an author holds whatever the
#: lifetime.
MAX_TTL_S: int = 31 * 86_400

#: Live ciphertext one author may hold here.
MAX_AUTHOR_LIVE_BYTES: int = 256 * 1024 * 1024

#: Blobs one author may upload per hour (a moment has ≤ 2: file + poster).
MAX_UPLOADS_PER_HOUR: int = 60

#: Expired blobs removed per sweep.
PRUNE_BATCH: int = 500

#: Re-exported: the moment routes map it to their HTTP answer.
__all__ = ["GfsMomentMediaStore", "MediaRefused"]


class GfsMomentMediaStore:
    """Encrypted moment blobs: files under ``media_dir/moments``, metadata
    in ``gfs_moment_media``."""

    __slots__ = ("_files", "_repo")

    def __init__(self, repo: AbstractGfsMomentMediaRepo, media_dir: str | Path) -> None:
        self._repo = repo
        self._files = FsBlobFiles(Path(media_dir) / "moments")

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
        check_shape(blob_id, data)
        if not now < expires_at <= now + MAX_TTL_S:
            raise MediaRefused(422, "bad_expiry")
        await check_address(blob_id, data)
        held = await self._repo.get(blob_id)
        if held is not None:
            if held.author_user_id != author_user_id:
                raise MediaRefused(409, "blob_id_taken")
            return False
        live, recent = await self._repo.author_usage(author_user_id, since=now - 3600)
        if recent >= MAX_UPLOADS_PER_HOUR or live + len(data) > MAX_AUTHOR_LIVE_BYTES:
            raise MediaRefused(429, "author_media_quota")
        await self._files.write(blob_id, data)
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
        return await self._files.read(blob_id)

    async def delete(self, blob_id: str, *, author_user_id: str) -> bool:
        if not is_blob_id(blob_id):
            return False
        if not await self._repo.delete(blob_id, author_user_id=author_user_id):
            return False
        await self._files.remove(blob_id)
        return True

    async def prune_expired(self, now: int | None = None) -> int:
        now = int(time.time()) if now is None else now
        ids = await self._repo.expired(now, limit=PRUNE_BATCH)
        for blob_id in ids:
            await self._files.remove(blob_id)
        await self._repo.delete_ids(ids)
        return len(ids)

    async def purge_author(self, author_user_id: str) -> int:
        """Every blob of an author who left the directory."""
        ids = await self._repo.ids_for_author(author_user_id)
        for blob_id in ids:
            await self._files.remove(blob_id)
        await self._repo.delete_ids(ids)
        return len(ids)
