"""Follower side of encrypted moment media (§Momentum-public, v_63).

A public moment that names a ``media`` blob (inside its ciphertext) is
stored only once its file is here: :class:`MomentPublicMediaInbound` fetches
the blob from the GFS the moment came through, checks that its SHA-256 is
the address the author sealed, opens it with the moment's item key
(:func:`socialhome.moment_public_media.open_media` — bound to author, moment
and role), and keeps the file under the media dir as
``gm-<first 40 hex of the blob id>.<ext>`` (a video's poster under the same
stem with ``.webp``, the name the household derives for a video's poster).
Then the inbound stores the moment pointing at it.

Off the GFS socket: jobs run on a background worker (bounded queue,
``_stop: asyncio.Event`` lifecycle), so an 8 MB download never blocks the
frames behind it. Anything that doesn't check out — an unknown suite or
content type, a wrong address, a blob that doesn't open, a fetch that fails
— leaves the moment without its file, never with a broken or foreign one.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import pathlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiofiles
import aiofiles.os
import aiohttp
from cryptography.exceptions import InvalidTag

from ..moment_public_media import (
    MAX_BLOB_BYTES,
    MEDIA_CONTENT_TYPES,
    SUPPORTED_MEDIA_SUITES,
    is_blob_id,
    open_media,
)
from .inbound_media_store import media_file_path, publish_once, remove_quietly

if TYPE_CHECKING:
    from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo

log = logging.getLogger(__name__)

#: Fetch jobs waiting at most; past it a moment is stored without its file.
MAX_QUEUED_JOBS: int = 64

#: One blob download at most this long.
FETCH_TIMEOUT_S: float = 60.0


@dataclass(slots=True, frozen=True)
class MediaJob:
    """Fetch one moment's media, then call ``then(media_url, media_type)``
    (``(None, None)`` when there is none to keep)."""

    gfs_id: str
    author_user_id: str
    moment_id: str
    item_key: bytes
    media: dict[str, Any]
    then: Callable[[str | None, str | None], Awaitable[None]]


class MomentPublicMediaInbound:
    """Fetch, verify and keep a followed author's encrypted moment media."""

    __slots__ = (
        "_dir",
        "_gfs_repo",
        "_http_client",
        "_queue",
        "_stop",
        "_task",
    )

    def __init__(
        self,
        *,
        gfs_repo: AbstractGfsConnectionRepo,
        media_dir: pathlib.Path,
        http_client: aiohttp.ClientSession | None = None,
    ) -> None:
        self._gfs_repo = gfs_repo
        self._dir = media_dir
        self._http_client = http_client
        self._queue: asyncio.Queue[MediaJob] = asyncio.Queue(maxsize=MAX_QUEUED_JOBS)
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def attach_session(self, session: aiohttp.ClientSession) -> None:
        if self._http_client is None:
            self._http_client = session

    # ── Worker ──────────────────────────────────────────────────────────

    def submit(self, job: MediaJob) -> bool:
        """Queue *job*; ``False`` when the queue is full (the caller stores
        the moment without its file)."""
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            log.warning(
                "moment_public.media: %d fetches already wait — moment %s "
                "is kept without its file",
                self._queue.qsize(),
                job.moment_id,
            )
            return False
        return True

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="moment-media-fetch")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            getter = asyncio.ensure_future(self._queue.get())
            stopper = asyncio.ensure_future(self._stop.wait())
            done, _ = await asyncio.wait(
                {getter, stopper}, return_when=asyncio.FIRST_COMPLETED
            )
            if getter not in done:
                getter.cancel()
                stopper.cancel()
                return
            stopper.cancel()
            job = getter.result()
            try:
                url, kind = await self.fetch(
                    gfs_id=job.gfs_id,
                    author_user_id=job.author_user_id,
                    moment_id=job.moment_id,
                    item_key=job.item_key,
                    media=job.media,
                )
                await job.then(url, kind)
            except Exception:  # pragma: no cover - defensive
                log.exception("moment_public.media: a fetch job failed")
            finally:
                self._queue.task_done()

    async def drain(self) -> None:
        """Wait until every queued job has run (shutdown, and callers that
        need the moments stored before going on)."""
        await self._queue.join()

    async def forget(self, media_url: str | None) -> None:
        """Remove a GFS-fetched file (and a video's poster) once its moment
        is deleted. Only names this service wrote (``gm-…``)."""
        name = (media_url or "").removeprefix("api/media/")
        stem, _, _ext = name.rpartition(".")
        if not stem.startswith("gm-"):
            return
        for candidate in (name, f"{stem}.webp"):
            path = media_file_path(self._dir, candidate)
            if path is not None:
                await remove_quietly(path)

    # ── One moment ──────────────────────────────────────────────────────

    async def fetch(
        self,
        *,
        gfs_id: str,
        author_user_id: str,
        moment_id: str,
        item_key: bytes,
        media: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        """The local ``(media_url, media_type)`` for a moment's media, or
        ``(None, None)``. Never raises."""
        kind = MEDIA_CONTENT_TYPES.get(str(media.get("content_type") or ""))
        blob_id = media.get("blob_id")
        poster_id = media.get("poster_blob_id")
        if (
            kind is None
            or media.get("media_suite") not in SUPPORTED_MEDIA_SUITES
            or not is_blob_id(blob_id)
            or (poster_id is not None and not is_blob_id(poster_id))
        ):
            log.info("moment_public.media: moment %s names no usable media", moment_id)
            return None, None
        assert isinstance(blob_id, str)
        ext, media_type = kind
        stem = f"gm-{blob_id[:40]}"
        name = f"{stem}.{ext}"
        path = media_file_path(self._dir, name)
        if path is None:  # pragma: no cover - the name is ours
            return None, None
        try:
            kept = await self._keep(
                gfs_id, blob_id, path, author_user_id, moment_id, item_key, "media"
            )
            if kept and media_type == "video" and isinstance(poster_id, str):
                poster = media_file_path(self._dir, f"{stem}.webp")
                if poster is not None:
                    await self._keep(
                        gfs_id,
                        poster_id,
                        poster,
                        author_user_id,
                        moment_id,
                        item_key,
                        "poster",
                    )
        except Exception:  # pragma: no cover - defensive
            log.exception("moment_public.media: keeping moment %s failed", moment_id)
            return None, None
        if not kept:
            return None, None
        return f"api/media/{name}", media_type

    async def _keep(
        self,
        gfs_id: str,
        blob_id: str,
        path: pathlib.Path,
        author_user_id: str,
        moment_id: str,
        item_key: bytes,
        role: str,
    ) -> bool:
        if await aiofiles.os.path.isfile(path):
            return True
        blob = await self._download(gfs_id, blob_id)
        if blob is None:
            return False
        digest = await asyncio.to_thread(lambda: hashlib.sha256(blob).hexdigest())
        if digest != blob_id:
            log.warning(
                "moment_public.media: blob for moment %s doesn't match its "
                "address — dropped",
                moment_id,
            )
            return False
        try:
            data = await asyncio.to_thread(
                lambda: open_media(
                    blob=blob,
                    item_key=item_key,
                    author_user_id=author_user_id,
                    moment_id=moment_id,
                    role=role,
                )
            )
        except (InvalidTag, ValueError) as exc:
            log.warning(
                "moment_public.media: %s of moment %s doesn't open: %s",
                role,
                moment_id,
                type(exc).__name__,
            )
            return False
        await aiofiles.os.makedirs(self._dir, exist_ok=True)
        tmp = self._dir / f".gm-{uuid.uuid4().hex}.part"
        try:
            async with aiofiles.open(tmp, "wb") as fh:
                await fh.write(data)
            await publish_once(tmp, path)
        finally:
            await remove_quietly(tmp)
        return True

    async def _download(self, gfs_id: str, blob_id: str) -> bytes | None:
        if self._http_client is None:
            return None
        conn = await self._gfs_repo.get(gfs_id)
        if conn is None or conn.status != "active":
            return None
        try:
            async with self._http_client.get(
                f"{conn.inbox_url}/gfs/moments/media/{blob_id}",
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT_S),
            ) as resp:
                if resp.status != 200:
                    log.info(
                        "moment_public.media: GFS %s has no blob for it — HTTP %d",
                        gfs_id,
                        resp.status,
                    )
                    return None
                chunks: list[bytes] = []
                total = 0
                while chunk := await resp.content.read(64 * 1024):
                    total += len(chunk)
                    if total > MAX_BLOB_BYTES:
                        return None
                    chunks.append(chunk)
        except (aiohttp.ClientError, TimeoutError) as exc:
            log.warning("moment_public.media: fetch from %s failed: %s", gfs_id, exc)
            return None
        return b"".join(chunks)
