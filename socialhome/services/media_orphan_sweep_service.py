"""Delete media-dir files that no DB row references (orphans).

The per-delete cleanup paths (DM, gallery, posts, highlights, moments) catch
the common cases, but some files still leak — most notably media whose owning
row was removed by a *remote* federation event. This sweep is the backstop:
it lists the media dir, and deletes any file that

  1. is NOT referenced by any DB row (see ``AbstractMediaReferenceRepo``), AND
  2. is older than :data:`GRACE_SECONDS` (so in-flight uploads / freshly
     created rows whose commit races the sweep are never reaped), AND
  3. is NOT a transient DM intermediate (``*.preview.webp`` / ``*.part<NNNN>``
     / ``*.assembled*``) — those aren't in any ``media_url`` column and are
     owned by ``dm_gc``; sweeping them would clobber in-progress transfers.

Only top-level regular files are considered — sub-directories (the DM
``.partial/`` staging area, the async-transcode ``transcode_src/`` source
stash) are skipped entirely by :meth:`sweep_once`. The transcode source
stash is reaped by its own pass, :meth:`sweep_transcode_src_once`, which the
scheduler runs alongside the top-level sweep each tick.

Fail-soft: any per-file error is logged and skipped so one bad file never
aborts the pass.
"""

from __future__ import annotations

import logging
import pathlib
import re
import stat
import time
from typing import TYPE_CHECKING

import aiofiles.os

from ..repositories.media_reference_repo import AbstractMediaReferenceRepo

if TYPE_CHECKING:
    from ..repositories.media_transcode_repo import AbstractMediaTranscodeRepo

log = logging.getLogger(__name__)

#: Sub-directory of the media root holding async-transcode source blobs.
_TRANSCODE_SRC_DIRNAME = "transcode_src"

#: Files younger than this (by mtime) are never swept — guards in-flight
#: uploads and rows whose DB commit races a sweep pass.
GRACE_SECONDS: int = 24 * 60 * 60

#: Transient DM artifacts that aren't referenced by a ``media_url`` column.
#: ``dm_gc`` owns these; the sweep must leave them alone.
_SKIP_RE = re.compile(r"(\.preview\.webp|\.part\d+|\.assembled)", re.IGNORECASE)


class MediaOrphanSweepService:
    """One-pass sweep of unreferenced media files."""

    __slots__ = ("_grace", "_media_dir", "_refs", "_transcode")

    def __init__(
        self,
        *,
        media_dir: pathlib.Path,
        reference_repo: AbstractMediaReferenceRepo,
        grace_seconds: int = GRACE_SECONDS,
        media_transcode_repo: AbstractMediaTranscodeRepo | None = None,
    ) -> None:
        self._media_dir = media_dir
        self._refs = reference_repo
        self._grace = grace_seconds
        self._transcode = media_transcode_repo

    async def sweep_once(self, *, now: float | None = None) -> int:
        """Delete orphaned media files; return the count removed."""
        now = time.time() if now is None else now
        try:
            entries = await aiofiles.os.listdir(self._media_dir)
        except FileNotFoundError:
            return 0
        except OSError as exc:  # pragma: no cover — defensive
            log.warning("media-sweep: listdir failed: %s", exc)
            return 0

        referenced = await self._refs.referenced_basenames()
        removed = 0
        for name in entries:
            if name in referenced or _SKIP_RE.search(name):
                continue
            path = self._media_dir / name
            try:
                st = await aiofiles.os.stat(path)
                # Skip directories (e.g. the DM ``.partial/`` staging area)
                # and anything still within the grace window.
                if stat.S_ISDIR(st.st_mode):
                    continue
                if now - st.st_mtime < self._grace:
                    continue
                await aiofiles.os.remove(path)
                removed += 1
            except FileNotFoundError:
                continue
            except OSError as exc:  # pragma: no cover — defensive
                log.debug("media-sweep: could not remove %s: %s", path, exc)
        if removed:
            log.info("media-sweep: removed %d orphaned media file(s)", removed)
        return removed

    async def sweep_transcode_src_once(self, *, now: float | None = None) -> int:
        """Delete leaked async-transcode source blobs; return count removed.

        On upload the raw source bytes are stashed at
        ``media_dir/transcode_src/<uuid>.bin`` and a ``media_transcode_jobs``
        row referencing that path is enqueued. The scheduler deletes the temp
        source on success and on permanent failure, so a blob only leaks in a
        narrow crash window (written but the row never processed/cleaned).
        This pass reaps those: any ``transcode_src`` file no job row
        references and older than the grace window.

        No-op when the service was built without a transcode repo (so a caller
        that doesn't wire one stays inert).
        """
        if self._transcode is None:
            return 0
        now = time.time() if now is None else now
        src_dir = self._media_dir / _TRANSCODE_SRC_DIRNAME
        try:
            entries = await aiofiles.os.listdir(src_dir)
        except FileNotFoundError:
            return 0
        except OSError as exc:  # pragma: no cover — defensive
            log.warning("media-sweep: transcode_src listdir failed: %s", exc)
            return 0

        active = await self._transcode.active_source_paths()
        removed = 0
        for name in entries:
            path = src_dir / name
            if str(path) in active:
                continue
            try:
                st = await aiofiles.os.stat(path)
                if stat.S_ISDIR(st.st_mode):
                    continue
                if now - st.st_mtime < self._grace:
                    continue
                await aiofiles.os.remove(path)
                removed += 1
            except FileNotFoundError:
                continue
            except OSError as exc:  # pragma: no cover — defensive
                log.debug("media-sweep: could not remove %s: %s", path, exc)
        if removed:
            log.info("media-sweep: removed %d leaked transcode source(s)", removed)
        return removed
