"""Author side of encrypted moment media (§Momentum-public, v_63).

A public moment's file is sealed **once** (:mod:`socialhome.moment_public_media`
— a key derived from the moment's item key, padded to a size bucket) and the
same blob is uploaded to every GFS the author publishes through that proved
``encrypted_moment_media``. The sealed moment body names it
(``media: {blob_id, media_suite, content_type, poster_blob_id?}``); followers
fetch it from their GFS. The author's home uplink carries the file once per
GFS, never once per follower.

* A file this build doesn't send (an unknown type, larger than
  :data:`~socialhome.moment_public_media.MAX_MEDIA_BYTES`, missing) leaves the
  moment without media — never the author's local reference, which would
  name nothing on a follower's side.
* A video still transcoding is *not ready*: :class:`MomentPublicOutbound`
  holds the moment until the transcode finishes.
* Uploads are header-signed by the author's household; deletes withdraw the
  blobs uploaded for that moment (best effort — every blob also expires with
  its moment).
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import aiofiles
import aiofiles.os
import aiohttp

from ..domain.moment import Moment
from ..moment_public_media import (
    CONTENT_TYPE_BY_EXT,
    MAX_MEDIA_BYTES,
    MEDIA_SUITE_AESGCM256_PADDED,
    blob_id_of,
    seal_media,
)
from ..moment_public_seal import sign_wire
from .inbound_media_store import local_media_ref, media_file_path

if TYPE_CHECKING:
    from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
    from .gfs_connection_service import GfsConnectionService

log = logging.getLogger(__name__)

#: Moments whose uploaded blobs are remembered for a delete.
MAX_REMEMBERED: int = 2_000

#: A video poster larger than this is not sent (posters are small WebP).
MAX_POSTER_BYTES: int = 1024 * 1024

#: Longest a blob (or a share copy) is asked to live on a GFS that proved
#: ``moment_media_30d`` (v_66): a moment's longest life, so a share link's
#: viewer still finds the picture as long as the moment lives.
MEDIA_KEEP_LONG_S: int = 30 * 86_400

#: …on an older GFS, which refuses more than 8 days. Followers fetch the file
#: when the moment reaches them and keep their own copy, so only a share
#: link outliving a week loses its picture there.
MEDIA_KEEP_SHORT_S: int = 7 * 86_400


@dataclass(slots=True)
class PreparedMedia:
    """A moment's sealed media: ``body`` goes inside the sealed moment,
    ``blobs`` (address → bytes) go to each GFS. ``ready`` is ``False``
    for a video still transcoding."""

    body: dict = field(default_factory=dict)
    blobs: dict[str, bytes] = field(default_factory=dict)
    ready: bool = True


class MomentPublicMediaOutbound:
    """Seal a public moment's file once; upload and withdraw it per GFS."""

    __slots__ = (
        "_dir",
        "_gfs",
        "_gfs_repo",
        "_http_client",
        "_own_instance_id",
        "_sent",
        "_signing_key",
    )

    def __init__(
        self,
        *,
        gfs_repo: AbstractGfsConnectionRepo,
        gfs_service: GfsConnectionService,
        media_dir: pathlib.Path,
        http_client: aiohttp.ClientSession | None = None,
    ) -> None:
        self._gfs_repo = gfs_repo
        self._gfs = gfs_service
        self._dir = media_dir
        self._http_client = http_client
        self._own_instance_id = ""
        self._signing_key: bytes | None = None
        #: ``(author, moment)`` → ``[(gfs_id, blob_id)]`` uploaded.
        self._sent: dict[tuple[str, str], list[tuple[str, str]]] = {}

    def attach_session(self, session: aiohttp.ClientSession) -> None:
        if self._http_client is None:
            self._http_client = session

    def attach_identity(self, *, own_instance_id: str, signing_key: bytes) -> None:
        self._own_instance_id = own_instance_id
        self._signing_key = signing_key

    async def supported(self, gfs_id: str) -> bool:
        return await self._gfs.encrypted_moment_media_supported(gfs_id)

    async def keep_until(
        self, gfs_id: str, moment_expires_at: int, *, now: int | None = None
    ) -> int:
        """How long *gfs_id* is asked to keep a blob of a moment that expires
        at *moment_expires_at* (Unix seconds): as long as the moment, at most
        :data:`MEDIA_KEEP_LONG_S` on a GFS that proved ``moment_media_30d``
        and :data:`MEDIA_KEEP_SHORT_S` on any other."""
        now = int(time.time()) if now is None else now
        cap = (
            MEDIA_KEEP_LONG_S
            if await self._gfs.moment_media_30d_supported(gfs_id)
            else MEDIA_KEEP_SHORT_S
        )
        return min(moment_expires_at, now + cap)

    # ── Seal ────────────────────────────────────────────────────────────

    async def prepare(self, moment: Moment, *, item_key: bytes) -> PreparedMedia | None:
        """Seal *moment*'s file (and a video's poster). ``None`` when there
        is nothing this build sends."""
        ref = local_media_ref(moment.media_url)
        if ref is None:
            return None
        name = ref.removeprefix("api/media/")
        stem, _, ext = name.rpartition(".")
        content_type = CONTENT_TYPE_BY_EXT.get(ext.lower())
        path = media_file_path(self._dir, name)
        if not stem or content_type is None or path is None:
            return None
        if not await aiofiles.os.path.isfile(path):
            if moment.media_type == "video":
                return PreparedMedia(ready=False)
            return None
        if (await aiofiles.os.stat(path)).st_size > MAX_MEDIA_BYTES:
            log.info(
                "moment_public.media: moment %s's file is over %d MB — "
                "followers get the moment without it",
                moment.id,
                MAX_MEDIA_BYTES // (1024 * 1024),
            )
            return None
        data = await _read(path)
        blob = await asyncio.to_thread(
            seal_media,
            item_key=item_key,
            author_user_id=moment.author_user_id,
            moment_id=moment.id,
            role="media",
            data=data,
        )
        bid = blob_id_of(blob)
        prepared = PreparedMedia(
            body={
                "blob_id": bid,
                "media_suite": MEDIA_SUITE_AESGCM256_PADDED,
                "content_type": content_type,
            },
            blobs={bid: blob},
        )
        poster = media_file_path(self._dir, f"{stem}.webp")
        if (
            moment.media_type == "video"
            and poster is not None
            and await aiofiles.os.path.isfile(poster)
            and (await aiofiles.os.stat(poster)).st_size <= MAX_POSTER_BYTES
        ):
            pblob = await asyncio.to_thread(
                seal_media,
                item_key=item_key,
                author_user_id=moment.author_user_id,
                moment_id=moment.id,
                role="poster",
                data=await _read(poster),
            )
            pid = blob_id_of(pblob)
            prepared.body["poster_blob_id"] = pid
            prepared.blobs[pid] = pblob
        return prepared

    # ── Upload / withdraw ───────────────────────────────────────────────

    async def upload(
        self,
        gfs_id: str,
        prepared: PreparedMedia,
        *,
        author_user_id: str,
        expires_at: int,
    ) -> bool:
        """PUT every blob to *gfs_id* — only when it proved
        ``encrypted_moment_media``. ``True`` when all were accepted.
        *expires_at* is the moment's own expiry; each GFS is asked to keep
        the blob for as much of it as it allows (:meth:`keep_until`)."""
        if self._http_client is None or self._signing_key is None:
            return False
        if not await self.supported(gfs_id):
            return False
        expires_at = await self.keep_until(gfs_id, expires_at)
        conn = await self._gfs_repo.get(gfs_id)
        if conn is None or conn.status != "active":
            return False
        for bid, blob in prepared.blobs.items():
            ts = int(time.time())
            signed = sign_wire(
                self._signing_key,
                {
                    "instance_id": self._own_instance_id,
                    "blob_id": bid,
                    "author_user_id": author_user_id,
                    "expires_at": expires_at,
                    "ts": ts,
                },
            )
            headers = {
                "X-SH-Instance": self._own_instance_id,
                "X-SH-Author": author_user_id,
                "X-SH-Expires": str(expires_at),
                "X-SH-Timestamp": str(ts),
                "X-SH-Signature": signed["signature"],
                "Content-Type": "application/octet-stream",
            }
            try:
                async with self._http_client.put(
                    f"{conn.inbox_url}/gfs/moments/media/{bid}",
                    data=blob,
                    headers=headers,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status >= 300:
                        log.warning(
                            "moment_public.media: GFS %s refused a blob — HTTP %d",
                            gfs_id,
                            resp.status,
                        )
                        return False
            except (aiohttp.ClientError, TimeoutError) as exc:
                log.warning("moment_public.media: upload to %s failed: %s", gfs_id, exc)
                return False
        return True

    def remember(
        self, author_user_id: str, moment_id: str, gfs_id: str, prepared: PreparedMedia
    ) -> None:
        if len(self._sent) >= MAX_REMEMBERED:
            self._sent.pop(next(iter(self._sent)))
        self._sent.setdefault((author_user_id, moment_id), []).extend(
            (gfs_id, bid) for bid in prepared.blobs
        )

    async def withdraw(self, author_user_id: str, moment_id: str) -> int:
        """Delete the blobs uploaded for a deleted moment. Returns how many
        deletes went out."""
        sent = self._sent.pop((author_user_id, moment_id), [])
        if not sent or self._http_client is None or self._signing_key is None:
            return 0
        done = 0
        for gfs_id, bid in sent:
            conn = await self._gfs_repo.get(gfs_id)
            if conn is None or conn.status != "active":
                continue
            body = sign_wire(
                self._signing_key,
                {
                    "instance_id": self._own_instance_id,
                    "author_user_id": author_user_id,
                    "blob_id": bid,
                },
            )
            try:
                async with self._http_client.post(
                    f"{conn.inbox_url}/gfs/moments/media/{bid}/delete",
                    json=body,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status < 300:
                        done += 1
            except (aiohttp.ClientError, TimeoutError) as exc:
                log.warning(
                    "moment_public.media: withdraw from %s failed: %s", gfs_id, exc
                )
        return done


async def _read(path: pathlib.Path) -> bytes:
    async with aiofiles.open(path, "rb") as fh:
        return await fh.read()
